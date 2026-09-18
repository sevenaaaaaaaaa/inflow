"""E2E 回归：独立数据目录 + 演示数据，避免污染单测环境"""
import pytest


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    from pathlib import Path

    import insflow.core.files as files_mod

    data_dir = tmp_path_factory.mktemp("e2e-data")
    files_mod.DATA_DIR = Path(data_dir)
    os.environ.setdefault("INSFLOW_MASTER_KEY", "e2e-key")
    os.environ.setdefault("INSFLOW_DISABLE_SCHEDULER", "1")
    os.environ.pop("INSFLOW_SAAS", None)

    import asyncio

    from insflow.core.store import Store, reset_store

    async def _boot():
        from insflow.core.entities import Workspace
        store = Store(db_path=Path(data_dir) / "e2e.db")
        await store.connect()
        await store.migrate()
        reset_store(store)
        await store.create_workspace(Workspace(id="e2e-ws", name="E2E"))
        from insflow.engine.demo import DemoSeeder
        await DemoSeeder("e2e-ws", days=20).seed()
        for i in range(12):
            for ch in ("search", "social", "direct"):
                await store.save_journey_event("e2e-ws", identity=f"u{i}",
                                               stage="visit", event="touch",
                                               props={"channel": ch})
        return store

    asyncio.get_event_loop().run_until_complete(_boot()) if False else asyncio.run(_boot())

    from fastapi.testclient import TestClient

    from insflow.server.app import app
    with TestClient(app) as c:
        yield c
