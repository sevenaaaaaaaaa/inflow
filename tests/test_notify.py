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
