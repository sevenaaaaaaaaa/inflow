"""语义检索（零依赖本地向量，可选 embedding API）

为什么不引向量库：本项目「文件优先、轻依赖」（README 原则 4）。单工作区万级洞察
用 SQLite + 本地哈希向量足够召回；需要更强语义时配 `INSFLOW_EMBED_MODEL`
走 embedding API，同表按 `model` 维度隔离（不同模型的向量永不混算）。

向量化（local-ngram-v1）：n-gram 哈希装桶（hashing trick）
- 英文/数字：整词 + 长词的 3-gram（拼写变体也能召回）
- 中文：单字 + **相邻**二字（不跨标点，避免"好的"这种假二字）
- 权重 1+log(tf)，按 token 类型加权，最后 L2 归一化 → 余弦 = 点积
- 哈希用 blake2b：Python 内置 `hash()` 带随机种子，跨进程不稳定，索引隔天就失效

能力边界（别当成真 embedding）：本地向量吃的是**字面 n-gram 重合**，
"竞品降价了吗"能召回"竞品A基础版降价 20%"，但"对手把价格压下来了"这种**同义改写**
召不回——需要真语义时配 `INSFLOW_EMBED_MODEL`（换模型后 sync 会自动重算全量）。

索引是**增量**的：`sync_workspace` 只处理 text_hash 变化的行，检索前自动调用，
所以新洞察写入后无需额外调度（本地向量化成本 ~微秒级/条）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime

DIM = 4096
LOCAL_MODEL = "local-ngram-v1"
MAX_INDEX_ROWS = 2000      # 单次同步处理的上限（按时间倒序取最近的）
MAX_SCAN_ROWS = 5000       # 单次检索扫描的向量行上限
MIN_SCORE = 0.04

KINDS = ("insight", "note", "report")

_WORD_RE = re.compile(r"[a-z0-9]+")


# ========== 向量化 ==========

def _weighted_tokens(text: str) -> list[tuple[str, float]]:
    """分词 →（token, 权重）：整词 1.0 / 词 3-gram 0.35 / 汉字二字 1.0 / 单字 0.45"""
    t = (text or "").lower()
    out: list[tuple[str, float]] = []
    for w in _WORD_RE.findall(t):
        out.append((w, 1.0))
        if len(w) > 4:
            out.extend((w[i:i + 3], 0.35) for i in range(len(w) - 2))
    prev = ""
    for c in t:
        if "一" <= c <= "鿿":
            out.append((c, 0.45))
            if prev:
                out.append((prev + c, 1.0))
            prev = c
        else:
            prev = ""
    return out


def _bucket(token: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big") % DIM


def embed_local(text: str) -> dict[int, float]:
    """本地稀疏向量（bucket → 权重，已 L2 归一化）"""
    tf: dict[int, float] = {}
    for token, weight in _weighted_tokens(text):
        b = _bucket(token)
        tf[b] = tf.get(b, 0.0) + weight
    vec = {b: 1.0 + math.log(v) for b, v in tf.items() if v > 0}
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm <= 0:
        return {}
    return {b: v / norm for b, v in vec.items()}


async def embed(text: str) -> tuple[str, dict | list]:
    """向量化：优先 embedding API（配置了才用），失败/未配置回落本地

    回落是**静默**的（只记日志）：检索是辅助能力，不该因为上游 429 就 500。
    """
    model = os.environ.get("INSFLOW_EMBED_MODEL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if model and api_key:
        try:
            return model, await _embed_api(text, model, api_key)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("insflow.semantic").warning("embedding API 回落本地：%s", e)
    return LOCAL_MODEL, embed_local(text)


async def _embed_api(text: str, model: str, api_key: str) -> list[float]:
    import httpx
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/embeddings",
            json={"model": model, "input": text[:8000]},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        vec = resp.json()["data"][0]["embedding"]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: dict | list, b: dict | list) -> float:
    """余弦（两侧均已归一化 → 点积）；稀疏/稠密混用时按稀疏侧遍历"""
    if isinstance(a, list) and isinstance(b, list):
        return float(sum(x * y for x, y in zip(a, b, strict=False)))
    if isinstance(a, list):
        a, b = b, a
    if isinstance(b, list):
        return float(sum(w * (b[i] if i < len(b) else 0.0) for i, w in a.items()))
    if len(a) > len(b):
        a, b = b, a
    return float(sum(w * b.get(i, 0.0) for i, w in a.items()))


def _decode_vector(raw: str) -> dict | list:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if isinstance(data, list):
        return data
    return {int(k): float(v) for k, v in data.items()}


def _encode_vector(vec: dict | list) -> str:
    if isinstance(vec, list):
        return json.dumps([round(v, 6) for v in vec])
    return json.dumps({str(k): round(v, 6) for k, v in vec.items()})


def text_fingerprint(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


# ========== 索引 ==========

def _insight_text(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in ("title", "summary", "type"))


def _note_text(row: dict) -> str:
    tags = row.get("tags_json") or "[]"
    if isinstance(tags, list):
        tags = " ".join(str(t) for t in tags)
    return " ".join([str(row.get("title") or ""), str(row.get("body") or ""), str(tags)])


async def _existing(store, workspace_id: str) -> dict[tuple[str, str], dict]:
    rows = await store._fetchall(
        "SELECT kind, ref_id, text_hash, model FROM embeddings WHERE workspace_id = ?",
        (workspace_id,))
    return {(r["kind"], r["ref_id"]): r for r in rows}


async def sync_workspace(workspace_id: str, *, limit: int = MAX_INDEX_ROWS) -> dict:
    """增量同步索引（只处理新增/正文变化的行）；返回 {indexed, skipped, removed}"""
    from ..core.store import generate_id, get_store
    store = await get_store()
    existing = await _existing(store, workspace_id)
    now = datetime.now(UTC).isoformat()
    indexed = skipped = 0
    seen: set[tuple[str, str]] = set()

    target_model = os.environ.get("INSFLOW_EMBED_MODEL", "").strip() or LOCAL_MODEL
    sources = [
        ("insight", await store._fetchall(
            "SELECT id, title, summary, type FROM insights WHERE workspace_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (workspace_id, limit)), _insight_text),
        ("note", await store._fetchall(
            "SELECT id, title, body, tags_json FROM agent_notes WHERE workspace_id = ? "
            "ORDER BY updated_at DESC LIMIT ?", (workspace_id, limit)), _note_text),
    ]

    for kind, rows, to_text in sources:
        for row in rows:
            ref_id = str(row.get("id") or "")
            if not ref_id:
                continue
            seen.add((kind, ref_id))
            text = to_text(row).strip()
            fp = text_fingerprint(text)
            prev = existing.get((kind, ref_id))
            # 模型变了也要重算：否则换 embedding 模型后老向量被 model 过滤掉，检索直接空
            if prev and prev.get("text_hash") == fp and prev.get("model") == target_model:
                skipped += 1
                continue
            model, vec = await embed(text)
            title = str(row.get("title") or "")[:300]
            snippet = text[:300]
            if prev:
                await store._execute(
                    "UPDATE embeddings SET model = ?, dim = ?, text_hash = ?, title = ?, "
                    "snippet = ?, vector_json = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND kind = ? AND ref_id = ?",
                    (model, DIM if isinstance(vec, dict) else len(vec), fp, title,
                     snippet, _encode_vector(vec), now, workspace_id, kind, ref_id))
            else:
                await store._execute(
                    "INSERT INTO embeddings (id, workspace_id, kind, ref_id, model, dim, "
                    "text_hash, title, snippet, vector_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (generate_id(), workspace_id, kind, ref_id, model,
                     DIM if isinstance(vec, dict) else len(vec), fp, title, snippet,
                     _encode_vector(vec), now))
            indexed += 1

    # 源行已删除 → 清掉孤儿向量（否则检索会返回打不开的结果）
    removed = 0
    for (kind, ref_id) in list(existing):
        if kind in ("insight", "note") and (kind, ref_id) not in seen:
            await store._execute(
                "DELETE FROM embeddings WHERE workspace_id = ? AND kind = ? AND ref_id = ?",
                (workspace_id, kind, ref_id))
            removed += 1
    await store._db.commit()
    return {"indexed": indexed, "skipped": skipped, "removed": removed}


async def reindex(workspace_id: str) -> dict:
    """全量重建（换 embedding 模型后必须跑一次，否则新旧向量不可比）"""
    from ..core.store import get_store
    store = await get_store()
    await store._execute("DELETE FROM embeddings WHERE workspace_id = ?", (workspace_id,))
    await store._db.commit()
    return await sync_workspace(workspace_id)


async def stats(workspace_id: str) -> dict:
    from ..core.store import get_store
    store = await get_store()
    rows = await store._fetchall(
        "SELECT kind, model, COUNT(*) AS n FROM embeddings WHERE workspace_id = ? "
        "GROUP BY kind, model", (workspace_id,))
    return {
        "total": sum(int(r["n"]) for r in rows),
        "by_kind": {r["kind"]: int(r["n"]) for r in rows},
        "models": sorted({r["model"] for r in rows}),
        "dim": DIM,
    }


# ========== 检索 ==========

async def search(workspace_id: str, query: str, *, kinds: list[str] | None = None,
                 limit: int = 10, min_score: float = MIN_SCORE,
                 sync: bool = True) -> list[dict]:
    """语义检索：返回 [{kind, ref_id, title, snippet, score}]（按分数倒序）"""
    from ..core.store import get_store
    query = (query or "").strip()
    if not query:
        return []
    if sync:
        try:
            await sync_workspace(workspace_id)
        except Exception:  # noqa: BLE001 —— 索引失败不该让检索整体失败
            import logging
            logging.getLogger("insflow.semantic").warning("索引同步失败，用现有索引检索")

    model, qvec = await embed(query)
    if not qvec:
        return []
    store = await get_store()
    params: list = [workspace_id, model]
    sql = ("SELECT kind, ref_id, title, snippet, vector_json FROM embeddings "
           "WHERE workspace_id = ? AND model = ?")
    wanted = [k for k in (kinds or []) if k in KINDS]
    if wanted:
        sql += " AND kind IN (" + ",".join("?" for _ in wanted) + ")"
        params.extend(wanted)
    sql += " LIMIT ?"
    params.append(MAX_SCAN_ROWS)

    hits = []
    for row in await store._fetchall(sql, tuple(params)):
        score = cosine(qvec, _decode_vector(row["vector_json"]))
        if score >= min_score:
            hits.append({"kind": row["kind"], "ref_id": row["ref_id"],
                         "title": row["title"], "snippet": row["snippet"],
                         "score": round(score, 4)})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]


async def search_insights(workspace_id: str, query: str, limit: int = 8) -> dict:
    """洞察语义检索（Agent 工具用）：命中后补齐严重度/置信度，便于直接引用"""
    from ..core.store import get_store
    hits = await search(workspace_id, query, kinds=["insight"], limit=limit)
    store = await get_store()
    out = []
    for h in hits:
        ins = await store.get_insight(h["ref_id"])
        if not ins:
            continue
        out.append({
            "id": ins.id,
            "title": ins.title,
            "summary": ins.summary,
            "severity": ins.severity.value,
            "confidence": ins.confidence,
            "status": ins.status.value,
            "score": h["score"],
        })
    return {"query": query, "total": len(out), "insights": out}
