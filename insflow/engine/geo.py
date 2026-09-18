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
# 内置世界边界（Natural Earth 110m，公有领域；经 world-atlas 转为 TopoJSON，约 105KB）
BUILTIN_WORLD = "world"
BUNDLED_DIR = Path(__file__).parent.parent / "data"
BUILTIN_FILES = {BUILTIN_WORLD: "world-110m.topo.json"}
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


def parse_topojson(doc: dict, name_key: str = "") -> dict:
    """最小 TopoJSON 解码（arcs + transform → Polygon/MultiPolygon）

    只支持 world-atlas 这类「quantized topology」：transform 缩放平移 + 负索引引用
    （-1-i 表示第 i 条 arc 的逆序）。
    """
    if doc.get("type") != "Topology":
        raise GeoError("不是 TopoJSON（type 需为 Topology）")
    tr = doc.get("transform") or {}
    scale = tr.get("scale") or [1, 1]
    translate = tr.get("translate") or [0, 0]
    raw_arcs = doc.get("arcs") or []

    def decode_arc(idx: int) -> list[list[float]]:
        arc = raw_arcs[idx] if idx >= 0 else list(reversed(raw_arcs[-1 - idx]))
        x = y = 0
        out = []
        for pt in arc:
            x += pt[0]
            y += pt[1]
            lon, lat = x * scale[0] + translate[0], y * scale[1] + translate[1]
            # 跨 180° 经线：模运算折返到 [-180,180]（单纯减 360 不够：斐济等会累计超出）
            lon = ((lon + 180.0) % 360.0) - 180.0
            out.append([lon, lat])
        return out

    def ring(arc_idx: list[int]) -> list[list[float]]:
        pts: list[list[float]] = []
        for i in arc_idx:
            seg = decode_arc(i)
            pts.extend(seg if not pts else seg[1:])
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts

    def geom_of(g: dict):
        gtype = g.get("type")
        if gtype == "Polygon":
            return {"type": "Polygon", "coordinates": [ring(a) for a in g["arcs"]]}
        if gtype == "MultiPolygon":
            return {"type": "MultiPolygon",
                    "coordinates": [[ring(a) for a in poly] for poly in g["arcs"]]}
        return None

    objects = doc.get("objects") or {}
    target = objects.get("countries") or next(iter(objects.values()), None)
    if not target:
        raise GeoError("TopoJSON 缺少 objects")
    geoms = target.get("geometries") or [target]
    features = []
    for g in geoms:
        geometry = geom_of(g)
        if not geometry:
            continue
        props = g.get("properties") or {}
        label = ""
        for key in ([name_key] if name_key else []) + list(DEFAULT_NAME_KEYS):
            if key and props.get(key):
                label = str(props[key])
                break
        if not label:
            continue
        features.append({"label": label[:80], "geometry": geometry,
                         "properties": {k: str(v)[:120]
                                        for k, v in list(props.items())[:20]}})
    if not features:
        raise GeoError("TopoJSON 中没有可用的国家/地区要素")
    return {"features": features, "name_key": name_key or "name",
            "count": len(features), "format": "topojson"}


def parse_any(raw: bytes | str, name_key: str = "") -> dict:
    """自动识别 GeoJSON / TopoJSON"""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        doc = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GeoError(f"解析失败：{e}") from e
    if doc.get("type") == "Topology":
        return parse_topojson(doc, name_key)
    return parse_geojson(text, name_key)


def _decimate_ring(ring: list, min_step: float = 0.35) -> list:
    """按最小步长抽稀环上顶点（内置 110m 地图再压 60%+ 渲染体积）

    保留首尾与拐点近似（步长阈值法，非 Douglas-Peucker 但足够且 O(n)）。
    """
    if len(ring) <= 4:
        return ring
    out = [ring[0]]
    keep_sq = min_step * min_step
    for pt in ring[1:-1]:
        last = out[-1]
        if (pt[0] - last[0]) ** 2 + (pt[1] - last[1]) ** 2 >= keep_sq:
            out.append(pt)
    out.append(ring[-1])
    return out


def _decimate_feature(feature: dict, min_step: float = 0.6,
                      precision: int = 1) -> dict:
    """抽稀 + 降精度（内置图用）：0.1° ≈ 11km，国家/省级判断足够"""
    geom = feature.get("geometry") or {}
    if geom.get("type") == "Polygon":
        coords = [[[round(x, precision), round(y, precision)] for x, y in
                   _decimate_ring(r, min_step)] for r in geom.get("coordinates") or []]
    elif geom.get("type") == "MultiPolygon":
        coords = [[[[round(x, precision), round(y, precision)] for x, y in
                    _decimate_ring(r, min_step)] for r in poly]
                  for poly in geom.get("coordinates") or []]
    else:
        return feature
    feature = dict(feature)
    feature["geometry"] = {"type": geom["type"], "coordinates": coords}
    return feature


def _bbox_area(ring: list) -> float:
    lons = [pt[0] for pt in ring if len(pt) >= 2]
    lats = [pt[1] for pt in ring if len(pt) >= 2]
    if not lons or not lats:
        return 0.0
    return abs(max(lons) - min(lons)) * abs(max(lats) - min(lats))


def _prune_small_polys(feature: dict, *, min_area: float = 6.0) -> dict:
    """顶点级裁剪：去掉面积过小的岛屿（保留每个国家的主陆块）"""
    geom = feature.get("geometry") or {}
    if geom.get("type") != "MultiPolygon":
        return feature
    kept = [poly for poly in geom.get("coordinates") or []
            if poly and _bbox_area(poly[0]) >= min_area]
    if not kept:                      # 全是小岛：保留最大的一个，避免国家消失
        polys = geom.get("coordinates") or []
        if polys:
            kept = [max(polys, key=lambda poly: _bbox_area(poly[0]))]
    feature = dict(feature)
    feature["geometry"] = {"type": "MultiPolygon", "coordinates": kept}
    return feature


def builtin_dataset(name: str = BUILTIN_WORLD) -> dict | None:
    """读取内置边界数据集（当前仅 world/Natural Earth 110m，公有领域）"""
    fname = BUILTIN_FILES.get(name)
    if not fname:
        return None
    path = BUNDLED_DIR / fname
    if not path.exists():
        return None
    cached = _builtin_cache.get(name)
    if cached:
        return cached
    parsed = parse_topojson(json.loads(path.read_text(encoding="utf-8")))
    # 世界地图默认剔除南极洲（几乎恒为空数据，且会把纬度范围压到 -90 浪费版面）
    parsed["features"] = [f for f in parsed["features"]
                          if f["label"].lower() != "antarctica"]
    # 剔除极小岛屿多边形（面积阈值）：体积从 ~165KB 降到可用范围，且不影响国家级判断
    parsed["features"] = [_prune_small_polys(f, min_area=6.0)
                          for f in parsed["features"]]
    parsed["features"] = [f for f in parsed["features"]
                          if f["geometry"]["type"] != "MultiPolygon"
                          or f["geometry"]["coordinates"]]
    parsed["features"] = [_decimate_feature(f, min_step=0.6, precision=1)
                          for f in parsed["features"]]
    parsed["count"] = len(parsed["features"])
    parsed["builtin"] = True
    _builtin_cache[name] = parsed
    return parsed


_builtin_cache: dict[str, dict] = {}


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
    parsed = parse_any(raw, name_key)
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
    """读取数据集：内置（world）优先，其次工作区已保存的（失败返回 None → 页面降级网格）"""
    if name in BUILTIN_FILES:
        return builtin_dataset(name)
    path = dataset_path(workspace_id, name)
    if not path.exists():
        return None
    try:
        return parse_any(path.read_bytes())
    except GeoError:
        return None


def list_datasets(settings: dict | None) -> dict:
    """可用数据集：内置 world（始终可选）+ 工作区已导入的"""
    out = {name: {"source": "builtin", "name_key": "name", "builtin": True}
           for name, fname in BUILTIN_FILES.items()
           if (BUNDLED_DIR / fname).exists()}
    out.update(dict((settings or {}).get("geo_datasets") or {}))
    return out


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


# 常见别名（指标维度值 → 数据集 label），提高自动匹配命中率
ALIASES = {
    "美国": "United States of America", "us": "United States of America",
    "usa": "United States of America", "united states": "United States of America",
    "中国": "China", "cn": "China", "英国": "United Kingdom", "uk": "United Kingdom",
    "德国": "Germany", "法国": "France", "日本": "Japan", "韩国": "South Korea",
    "india": "India", "印度": "India", "brazil": "Brazil", "巴西": "Brazil",
    "canada": "Canada", "加拿大": "Canada", "澳大利亚": "Australia",
    "australia": "Australia", "russia": "Russia", "俄罗斯": "Russia",
    "新加坡": "Singapore", "singapore": "Singapore",
}


def match_score(dataset: dict, values: list[str]) -> int:
    """维度值与数据集 label 的匹配数（含别名；用于自动选图）"""
    labels = {str(f["label"]).lower(): f["label"] for f in dataset.get("features") or []}
    score = 0
    for v in values:
        key = str(v).strip().lower()
        if key in labels:
            score += 1
            continue
        alias = ALIASES.get(key)
        if alias and alias.lower() in labels:
            score += 1
    return score


def pick_dataset(datasets: dict, loaded: dict[str, dict],
                 values: list[str]) -> tuple[str, dict | None]:
    """按匹配度选数据集：命中≥1 才用（否则返回空 → 页面回退网格地图）"""
    best_name, best_score, best_ds = "", 0, None
    for name, ds in loaded.items():
        if not ds:
            continue
        score = match_score(ds, values)
        if score > best_score:
            best_name, best_score, best_ds = name, score, ds
    return best_name, best_ds


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
