"""GeoJSON 数据集管理（精确边界地图的数据面）

设计取舍（零依赖）：
- 不内置世界/中国边界数据（体积大且版权/精度难保证）；改为**用户提供 GeoJSON**
  （URL 抓取或直接上传），服务端缓存到工作区设置 + 本地文件。
- 属性匹配用 `properties.name`（可配置 `name_key`，默认 name/NAME/name_zh），
  与指标维度值（province/country）做名称对齐；匹配不上的区域按"无数据"渲染。

安全：URL 抓取限制协议（http/https）、大小（默认 ≤ 5MB）、超时；解析失败明确报错。
"""

import json
import re
import time
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
DEFAULT_NAME_KEYS = ("name", "NAME", "Name", "name_zh", "province", "admin")


class GeoError(Exception):
    pass


def _cache_dir() -> Path:
    from ..core.files import DATA_DIR
    d = Path(DATA_DIR or ".") / "geo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dataset_path(workspace_id: str, name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)[:48] or "dataset"
    return _cache_dir() / f"{workspace_id[:32]}__{safe}.geojson"


def parse_geojson(raw: bytes | str, name_key: str = "") -> dict:
    """解析并校验 GeoJSON → {"features": [...], "name_key": ...}"""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as e:
        raise GeoError("GeoJSON 必须是 UTF-8 文本") from e
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise GeoError(f"GeoJSON 过大（>{MAX_BYTES // 1024 // 1024}MB）")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise GeoError(f"GeoJSON 解析失败：{e}") from e
    dtype = doc.get("type")
    if dtype == "FeatureCollection":
        features = doc.get("features") or []
    elif dtype == "Feature":
        features = [doc]
    else:
        raise GeoError("只支持 FeatureCollection / Feature")
    cleaned = []
    for f in features[:5000]:
        geom = (f or {}).get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        props = f.get("properties") or {}
        label = ""
        for key in ([name_key] if name_key else []) + list(DEFAULT_NAME_KEYS):
            if key and props.get(key):
                label = str(props[key])
                break
        if not label:
            continue
        cleaned.append({"label": label[:80], "geometry": geom,
                        "properties": {k: str(v)[:120] for k, v in list(props.items())[:20]}})
    if not cleaned:
        raise GeoError("没有可用的多边形要素（需要 Polygon/MultiPolygon + name 属性）")
    return {"features": cleaned, "name_key": name_key or "name",
            "count": len(cleaned)}


async def fetch_geojson(url: str, timeout: float = 20.0) -> bytes:
    """抓取远端 GeoJSON（限协议/大小）"""
    if not re.match(r"^https?://", url or ""):
        raise GeoError("只允许 http/https 的 GeoJSON 地址")
    import httpx
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_BYTES:
            raise GeoError(f"远端 GeoJSON 过大（>{MAX_BYTES // 1024 // 1024}MB）")
        return resp.content


async def save_dataset(workspace_id: str, name: str, *, url: str = "",
                       content: bytes | str = b"", name_key: str = "") -> dict:
    """保存数据集（URL 抓取或直接上传），写入缓存文件 + 工作区设置登记"""
    from ..core.store import get_store
    raw = content or (await fetch_geojson(url) if url else b"")
    if not raw:
        raise GeoError("需要 url 或 content")
    parsed = parse_geojson(raw, name_key)
    path = dataset_path(workspace_id, name)
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    store = await get_store()
    ws = await store.get_workspace(workspace_id)
    if not ws:
        raise GeoError("工作区不存在")
    settings = dict(ws.settings_json or {})
    datasets = dict(settings.get("geo_datasets") or {})
    datasets[name] = {"source": url or "upload", "count": parsed["count"],
                      "name_key": parsed["name_key"], "bytes": path.stat().st_size,
                      "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    settings["geo_datasets"] = datasets
    ws.settings_json = settings
    await store.update_workspace(ws)
    return {"ok": True, "name": name, **datasets[name]}


async def load_dataset(workspace_id: str, name: str) -> dict | None:
    """读取已保存数据集（解析结果），失败返回 None（页面降级为网格地图）"""
    path = dataset_path(workspace_id, name)
    if not path.exists():
        return None
    try:
        return parse_geojson(path.read_bytes())
    except GeoError:
        return None


def list_datasets(settings: dict | None) -> dict:
    return dict((settings or {}).get("geo_datasets") or {})


def project(geometry: dict, bounds: tuple[float, float, float, float],
            width: float, height: float, pad: float = 6.0) -> str:
    """等距圆柱投影（lon/lat → SVG path，支持 Polygon/MultiPolygon；跨 180° 简化处理）"""
    min_lon, min_lat, max_lon, max_lat = bounds
    span_lon = max(1e-6, max_lon - min_lon)
    span_lat = max(1e-6, max_lat - min_lat)

    def xy(lon: float, lat: float) -> tuple[float, float]:
        x = pad + (lon - min_lon) / span_lon * (width - 2 * pad)
        y = pad + (max_lat - lat) / span_lat * (height - 2 * pad)
        return round(x, 2), round(y, 2)

    def ring_path(ring: list) -> str:
        if not ring:
            return ""
        pts = [xy(float(p[0]), float(p[1])) for p in ring if len(p) >= 2]
        if not pts:
            return ""
        d = f"M{pts[0][0]},{pts[0][1]}"
        for x, y in pts[1:]:
            d += f"L{x},{y}"
        return d + "Z"

    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    parts: list[str] = []
    if gtype == "Polygon":
        parts = [ring_path(r) for r in coords]
    elif gtype == "MultiPolygon":
        for poly in coords:
            parts.extend(ring_path(r) for r in poly)
    return " ".join(p for p in parts if p)


def bounds_of(features: list[dict]) -> tuple[float, float, float, float]:
    """数据集经纬度范围（用于自适应投影）"""
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    for f in features:
        geom = f["geometry"]
        polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
                 else geom["coordinates"])
        for poly in polys:
            for ring in poly:
                for pt in ring:
                    if len(pt) < 2:
                        continue
                    min_lon, max_lon = min(min_lon, pt[0]), max(max_lon, pt[0])
                    min_lat, max_lat = min(min_lat, pt[1]), max(max_lat, pt[1])
    if min_lon == float("inf"):
        return (-180.0, -90.0, 180.0, 90.0)
    return (min_lon, min_lat, max_lon, max_lat)
