"""测试飞书/Slack 告警出站"""

import pytest

from insflow.actions.notify import FeishuNotifyAdapter, SlackNotifyAdapter
from insflow.actions.router import get_action_router


class FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, resp, capture):
        self._resp = resp
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json=None, timeout=None):
        self._capture["url"] = url
        self._capture["json"] = json
        return self._resp


@pytest.fixture
def router():
    return get_action_router()


@pytest.mark.asyncio
async def test_builtin_types_including_notify(router):
    types = router.list_types()
    assert "feishu.notify" in types
    assert "slack.notify" in types


@pytest.mark.asyncio
async def test_feishu_notify(monkeypatch, tmp_path):
    import insflow.actions.notify as mod
    import insflow.core.files as files_mod
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    capture = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, timeout=None):
            capture["url"] = url
            capture["json"] = json
            return FakeResp(200, {"code": 0, "msg": "success"})

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    adapter = FeishuNotifyAdapter()
    result = await adapter.execute(
        {
            "action_type": "feishu.notify",
            "target_ref": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123",
            "title": "竞品降价",
            "summary": "竞品A降价20%",
            "severity": "high",
        },
        None,
    )
    assert result["ok"] is True
    assert capture["url"].endswith("hook/abc123")
    assert capture["json"]["msg_type"] == "interactive"


@pytest.mark.asyncio
async def test_feishu_invalid_url():
    adapter = FeishuNotifyAdapter()
    result = await adapter.execute(
        {"action_type": "feishu.notify", "target_ref": "https://evil.com/hook"},
        None,
    )
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_slack_notify(monkeypatch, tmp_path):
    import insflow.actions.notify as mod
    import insflow.core.files as files_mod
    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    capture = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, timeout=None):
            capture["url"] = url
            capture["json"] = json
            return FakeResp(200, {"ok": True})

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    adapter = SlackNotifyAdapter()
    result = await adapter.execute(
        {
            "action_type": "slack.notify",
            "target_ref": "https://hooks.slack.com/services/T00/B00/xyz",
            "title": "流量下降",
            "summary": "本周流量下降40%",
        },
        None,
    )
    assert result["ok"] is True
    assert "hooks.slack.com" in capture["url"]
    assert "流量下降" in capture["json"]["text"]


class TestEmailNotify:
    async def test_email_requires_recipient(self):
        from insflow.actions.notify import EmailNotifyAdapter
        result = await EmailNotifyAdapter().execute(
            {"action_type": "email.send", "target_ref": "not-an-email"}, None)
        assert result["ok"] is False
        assert "收件地址" in result["detail"]

    async def test_email_requires_smtp(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("SMTP_FROM", raising=False)
        from insflow.actions.notify import EmailNotifyAdapter
        result = await EmailNotifyAdapter().execute(
            {"action_type": "email.send", "target_ref": "u@x.com"}, None)
        assert result["ok"] is False
        assert "SMTP 未配置" in result["detail"]

    async def test_email_send_success(self, monkeypatch):
        """SMTP 发送走线程，校验邮件内容"""
        monkeypatch.setenv("SMTP_HOST", "smtp.test")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("SMTP_FROM", "insight@test.com")
        monkeypatch.setenv("SMTP_USER", "u")
        monkeypatch.setenv("SMTP_PASSWORD", "p")

        captured = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                captured["host"], captured["port"] = host, port
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def starttls(self): captured["tls"] = True
            def login(self, u, p): captured["login"] = (u, p)
            def send_message(self, msg):
                captured["subject"] = msg["Subject"]
                captured["to"] = msg["To"]
                captured["body"] = msg.get_content()

        import smtplib
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

        from insflow.actions.notify import EmailNotifyAdapter
        result = await EmailNotifyAdapter().execute({
            "action_type": "email.send", "target_ref": "user@x.com",
            "title": "舆情负面预警：某品牌", "summary": "负向占比 45%", "severity": "high",
        }, None)

        assert result["ok"] is True
        assert captured["host"] == "smtp.test"
        assert captured["tls"] is True
        assert captured["to"] == "user@x.com"
        assert "某品牌" in captured["subject"] and "HIGH" in captured["subject"]
