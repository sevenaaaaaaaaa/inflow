"""测试 mflow.create_content 适配器（loop/create + 轮询 + 取稿）"""

import pytest

from insflow.actions.mflow_adapter import MFlowCreateContentAdapter, MFlowRegisterTopicAdapter
from insflow.actions.router import ActionContext, get_action_router
from insflow.integrations.mflow import MFlowClient


class FakeResp:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data or {}
        self.headers = headers or {}

    def raise_for_status(self):
        assert self.status_code < 400

    def json(self):
        return self._data


@pytest.fixture
def fake_mflow(monkeypatch, tmp_path):
    """Fake MFlow HTTP 层：记录调用 + 可编排响应"""
    import insflow.core.files as files_mod
    import insflow.integrations.mflow.client as mod

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    calls = []
    state = {"loop_status": "queued"}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None, timeout=None):
            calls.append(("POST", url, json))
            if url.endswith("/api/login"):
                return FakeResp(200, {"ok": True}, headers={"set-cookie": "session=abc; Path=/"})
            if url.endswith("/api/loop/create"):
                return FakeResp(200, {"ok": True, "id": "loop-42", "queued": True})
            if url.endswith("/api/item/upsert"):
                return FakeResp(200, {"ok": True, "item_id": "itm-7"})
            if url.endswith("/api/item/advance"):
                return FakeResp(200, {"ok": True})
            return FakeResp(200, {"ok": False})

        async def get(self, url, params=None, headers=None, timeout=None):
            calls.append(("GET", url, params))
            if url.endswith("/api/loop/detail"):
                status = state["loop_status"]
                state["loop_status"] = "done"  # 第一次查询后完成（模拟异步产稿）
                return FakeResp(200, {"ok": True, "id": params["id"],
                                      "status": status,
                                      "draft_path": "output/draft.md"})
            if url.endswith("/api/read"):
                return FakeResp(200, {"ok": True, "content": "# 草稿内容"})
            return FakeResp(200, {"ok": False})

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())

    # 加速轮询
    import asyncio
    real_sleep = asyncio.sleep

    async def fast_sleep(sec):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    return {"calls": calls, "state": state}


class TestMFlowCreateContent:
    async def test_full_flow(self, fake_mflow):
        client = MFlowClient(base_url="http://mflow.test", password="pw")
        adapter = MFlowCreateContentAdapter(client)

        result = await adapter.execute(
            {
                "action_type": "mflow.create_content",
                "title": "竞品降价应对",
                "summary": "竞品A降价20%，产出对比内容",
                "params_json": {"fetch_draft": True, "max_polls": 3},
            },
            ActionContext(workspace_id="ws", insight_id="i1"),
        )

        assert result["ok"] is True
        assert result["ref"] == "mflow:loop:loop-42"
        assert "draft_chars" in result["detail"]

        urls = [c[1] for c in fake_mflow["calls"]]
        assert any(u.endswith("/api/login") for u in urls)
        assert any(u.endswith("/api/loop/create") for u in urls)
        assert any(u.endswith("/api/loop/detail") for u in urls)
        assert any(u.endswith("/api/read") for u in urls)

    async def test_not_configured(self):
        client = MFlowClient(base_url="", password="")
        adapter = MFlowCreateContentAdapter(client)
        result = await adapter.execute({"action_type": "mflow.create_content"}, None)
        assert result["ok"] is False

    async def test_never_publishes(self, fake_mflow):
        """铁律：绝不调用发布接口"""
        client = MFlowClient(base_url="http://mflow.test", password="pw")
        adapter = MFlowCreateContentAdapter(client)
        await adapter.execute(
            {"action_type": "mflow.create_content", "params_json": {"max_polls": 1}},
            ActionContext(workspace_id="ws", insight_id="i1"),
        )
        urls = [c[1] for c in fake_mflow["calls"]]
        assert not any("publish" in u for u in urls)


class TestMFlowRegisterTopic:
    async def test_upsert_and_advance(self, fake_mflow):
        client = MFlowClient(base_url="http://mflow.test", password="pw")
        adapter = MFlowRegisterTopicAdapter(client)

        result = await adapter.execute(
            {
                "action_type": "mflow.register_topic",
                "title": "关键词机会",
                "params_json": {"brief": "围绕机会词产稿"},
            },
            ActionContext(workspace_id="ws", insight_id="i9"),
        )

        assert result["ok"] is True
        assert result["ref"] == "mflow:item:itm-7"

    async def test_registered_in_router(self):
        types = get_action_router().list_types()
        assert "mflow.create_content" in types
        assert "mflow.register_topic" in types
