"""Agent 工作区记忆（agent_notes）：让多轮对话与定时任务共享上下文

- 写入：按 note_key 去重，更新即 version+1（可追溯）
- 读取：关键词检索（零依赖：英文词 + 中文按字符弱匹配）
- 注入：ask 时把命中的记忆放进 system prompt（本地数据，不外传）
"""

import re

MAX_INJECT = 5


def slugify(title: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (title or "").strip().lower()).strip("-")
    return (s or "note")[:80]


async def save_note(workspace_id: str, *, title: str, body: str,
                    tags: list | None = None, citations: list | None = None,
                    author: str = "agent", note_key: str = "") -> dict:
    """新建或更新一条记忆（note_key 相同 → 版本 +1）

    citations 支持 ["insight_id"] 或 [{"insight_id": ..., "title": ...}] 两种写法。
    """
    from ..core.store import get_store
    cites = None if citations is None else [
        {"insight_id": c} if isinstance(c, str) else c for c in citations]
    store = await get_store()
    return await store.upsert_agent_note(
        workspace_id, note_key=note_key or slugify(title), title=title,
        body=body, tags=tags, citations=cites, author=author)


async def recall_notes(workspace_id: str, query: str = "",
                       limit: int = 10) -> list[dict]:
    from ..core.store import get_store
    store = await get_store()
    return await store.search_agent_notes(workspace_id, query, limit)


async def list_notes(workspace_id: str, limit: int = 50) -> list[dict]:
    from ..core.store import get_store
    store = await get_store()
    return await store.list_agent_notes(workspace_id, limit)


async def delete_note(workspace_id: str, note_id: str) -> bool:
    from ..core.store import get_store
    store = await get_store()
    return await store.delete_agent_note(workspace_id, note_id)


async def memory_context(workspace_id: str, question: str,
                         limit: int = MAX_INJECT) -> str:
    """注入 system prompt 的记忆片段（无命中/出错返回空串，不阻塞问答）"""
    try:
        notes = await recall_notes(workspace_id, question, limit=limit)
    except Exception:
        return ""
    if not notes:
        return ""
    lines = ["工作区记忆（此前沉淀的上下文，可引用；与工具返回冲突时以工具为准）："]
    for n in notes:
        cites = n.get("citations_json") or []
        cite_txt = ""
        if cites:
            ids = [str(c.get("insight_id") or c) for c in cites[:3]]
            cite_txt = f"（引用 {', '.join(ids)}）"
        lines.append(f"- [{n.get('note_key')} v{n.get('version', 1)}] "
                     f"{n.get('title')}: {(n.get('body') or '')[:200]}{cite_txt}")
    return "\n".join(lines)
