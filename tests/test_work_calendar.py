"""测试工作日历（工作日执行限制 + 夜间窗口 + 本地时区换算）"""

from datetime import date

import insflow.core.work_calendar as wc


class TestIsWorkday:
    def test_normal_weekday(self):
        # 2026-10-09 周五（无节假日）
        assert wc.is_workday(date(2026, 10, 9)) is True

    def test_weekend(self):
        assert wc.is_workday(date(2026, 10, 10)) is False  # 周六
        assert wc.is_workday(date(2026, 10, 11)) is False  # 周日

    def test_statutory_holiday(self):
        assert wc.is_workday(date(2026, 10, 1)) is False  # 国庆
        assert wc.is_workday(date(2026, 1, 1)) is False   # 元旦

    def test_workday_swap(self):
        # 调休补班日：周末但算工作日（2025-09-28 周日补班）
        assert wc.is_workday(date(2025, 9, 28)) is True
        assert wc.is_workday(date(2026, 2, 14)) is True   # 2026 春节补班

    def test_env_overrides(self, monkeypatch):
        d = date(2026, 10, 10)  # 周六
        monkeypatch.setenv("INSFLOW_EXTRA_WORKDAYS", "2026-10-10")
        assert wc.is_workday(d) is True
        monkeypatch.setenv("INSFLOW_EXTRA_WORKDAYS", "")
        monkeypatch.setenv("INSFLOW_EXTRA_HOLIDAYS", "2026-10-10")
        assert wc.is_workday(d) is False

    def test_iso_string_input(self):
        assert wc.is_workday("2026-10-09") is True
        assert wc.is_workday("2026-10-10") is False


class TestNightWindow:
    def test_next_night_end_before_6(self):
        # 06:00 之前 → 今天 06:00
        tz = wc.zoneinfo.ZoneInfo(wc.local_tzname())
        now = wc.datetime(2026, 10, 9, 2, 0, tzinfo=tz)
        end = wc.next_night_end(now)
        assert end.hour == 6 and end.day == 9

    def test_next_night_end_after_6(self):
        tz = wc.zoneinfo.ZoneInfo(wc.local_tzname())
        now = wc.datetime(2026, 10, 9, 8, 0, tzinfo=tz)
        end = wc.next_night_end(now)
        assert end.hour == 6 and end.day == 10

    def test_seconds_until_night_end_positive(self):
        assert wc.seconds_until_night_end() > 0


class TestLocalCron:
    def test_shanghai_midnight_is_16_utc(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_LOCAL_TZ", "Asia/Shanghai")
        # 本地 00:00 = UTC 前一日 16:00（cron 五段 = 分钟 小时 日 月 周）
        assert wc.local_hour_to_utc_cron(0, 0) == "0 16 * * *"

    def test_shanghai_8am_is_0_utc(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_LOCAL_TZ", "Asia/Shanghai")
        assert wc.local_hour_to_utc_cron(8, 0) == "0 0 * * *"

    def test_utc_tz_identity(self, monkeypatch):
        monkeypatch.setenv("INSFLOW_LOCAL_TZ", "UTC")
        assert wc.local_hour_to_utc_cron(8, 30) == "30 8 * * *"


class TestDigestCrons:
    def test_default_morning_evening(self, monkeypatch):
        monkeypatch.delenv("INSFLOW_DIGEST_HOURS", raising=False)
        monkeypatch.setenv("INSFLOW_LOCAL_TZ", "Asia/Shanghai")
        from insflow.engine.email_digest import digest_crons
        crons = dict(digest_crons())
        assert crons["morning"] == "0 0 * * *"    # 本地 08:00 → UTC 00:00
        assert crons["evening"] == "0 10 * * *"   # 本地 18:00 → UTC 10:00
