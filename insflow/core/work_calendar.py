"""Insight Flow 工作日历（工作日执行限制 + 夜间窗口）

需求：自动化任务只在工作日执行，周六日与法定节假日不执行；
非重要任务固定在本地凌晨 00:00 执行，早晨 06:00 前必须结束。

设计（对齐「文件优先、轻依赖」原则）：
- 不引入第三方日历库（chinese_calendar 等会随年份停更）。
- 内置中国法定节假日表（best effort，覆盖 2025-2026），可用环境变量覆盖：
  - INSFLOW_EXTRA_HOLIDAYS：额外放假日（逗号分隔 YYYY-MM-DD）
  - INSFLOW_EXTRA_WORKDAYS：调休补班日（逗号分隔 YYYY-MM-DD）
- 时区：INSFLOW_LOCAL_TZ（默认 Asia/Shanghai）。调度器走 UTC，
  「本地日期/夜间窗口」一律经本模块换算，避免 UTC 偏移导致的日期错位。
"""

import os
import zoneinfo
from datetime import date, datetime, time, timedelta

# 中国法定节假日（放假当天，含调休连休）；best effort，缺漏用环境变量补
# 来源：国务院办公厅发布的放假安排
CN_HOLIDAYS: set[str] = {
    # 2025
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
    "2025-05-31", "2025-06-01", "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
    "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026（国务院办公厅 2025 年底发布口径）
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}

# 调休补班日（周末但算工作日）
CN_WORKDAY_SWAPS: set[str] = {
    "2025-01-26", "2025-02-08",
    "2025-04-27",
    "2025-09-28", "2025-10-11",
    "2026-02-14", "2026-02-28",
}


def local_tzname() -> str:
    """本地业务时区（INSFLOW_LOCAL_TZ，默认 Asia/Shanghai）"""
    return os.environ.get("INSFLOW_LOCAL_TZ", "Asia/Shanghai") or "Asia/Shanghai"


def local_now() -> datetime:
    """当前本地业务时区时间"""
    return datetime.now(zoneinfo.ZoneInfo(local_tzname()))


def local_date(now: datetime | None = None) -> date:
    """当前本地日期（跨时区统计的统一口径）"""
    return (now or local_now()).date()


def _parse_env_dates(name: str) -> set[date]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return set()
    out: set[date] = set()
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.add(date.fromisoformat(piece))
        except ValueError:
            continue
    return out


def is_workday(d: date | str | None = None) -> bool:
    """是否工作日：周一至周五，剔除法定节假日，加上调休补班日

    覆盖优先级：环境变量（INSFLOW_EXTRA_HOLIDAYS / INSFLOW_EXTRA_WORKDAYS）
    > 内置节假日表 > 普通周中判定
    """
    if d is None:
        d = local_date()
    if isinstance(d, str):
        d = date.fromisoformat(d)

    extra_work = _parse_env_dates("INSFLOW_EXTRA_WORKDAYS")
    if d in extra_work:
        return True
    extra_holiday = _parse_env_dates("INSFLOW_EXTRA_HOLIDAYS")
    if d in extra_holiday:
        return False
    if d.isoformat() in CN_HOLIDAYS:
        return False
    if d.isoformat() in CN_WORKDAY_SWAPS:
        return True
    return d.weekday() < 5  # 0=周一


# ========== 夜间窗口（非重要任务：00:00 启动，06:00 前必须结束）==========

NIGHT_START = time(0, 0)
NIGHT_END = time(6, 0)


def is_night_window(now: datetime | None = None) -> bool:
    """当前是否处于夜间执行窗口（本地 00:00–06:00，含边界）"""
    t = (now or local_now()).time()
    return NIGHT_START <= t <= NIGHT_END


def next_night_end(now: datetime | None = None) -> datetime:
    """下一个 06:00（本地时区 aware）——非重要任务的强制截止时刻

    06:00 之前的调用返回今天 06:00；06:00 之后返回明天 06:00。
    """
    tz = zoneinfo.ZoneInfo(local_tzname())
    now = now or local_now()
    end = now.astimezone(tz).replace(hour=6, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return end


def seconds_until_night_end(now: datetime | None = None) -> float:
    """距下一个 06:00（本地）的秒数（非重要任务 asyncio 超时用）"""
    return max(1.0, (next_night_end(now) - (now or local_now())).total_seconds())


def local_hour_to_utc_cron(hour: int, minute: int = 0) -> str:
    """「本地每日 HH:MM」→ UTC cron（调度器统一走 UTC）

    用当前时刻的时区偏移换算（每日固定本地时刻；DST 切换日有 ±1h 误差，可接受）。
    """
    tz = zoneinfo.ZoneInfo(local_tzname())
    offset = datetime.now(tz).utcoffset() or timedelta(0)
    total_min = (hour * 60 + minute - int(offset.total_seconds() // 60)) % 1440
    # cron 五段 = 分钟 小时 日 月 周（第一段是分钟！）
    return f"{total_min % 60} {total_min // 60} * * *"
