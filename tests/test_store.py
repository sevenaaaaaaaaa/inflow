"""测试核心实体和存储"""

import pytest
import asyncio
from pathlib import Path

from insflow.core.entities import (
    Workspace,
    Insight,
    InsightSeverity,
    InsightStatus,
    WorkspaceStage,
    MaturityLevel,
)
from insflow.core.store import Store


@pytest.fixture
def tmp_db(tmp_path):
    """临时数据库"""
    return tmp_path / "test.db"


@pytest.fixture
async def store(tmp_db):
    """测试存储实例"""
    s = Store(db_path=tmp_db)
    await s.connect()
    await s.migrate()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_create_workspace(store):
    """测试创建工作区"""
    ws = Workspace(name="Test Workspace")
    ws = await store.create_workspace(ws)
    assert ws.id is not None
    assert ws.name == "Test Workspace"
    assert ws.stage == WorkspaceStage.S0


@pytest.mark.asyncio
async def test_get_workspace(store):
    """测试获取工作区"""
    ws = Workspace(name="Test")
    ws = await store.create_workspace(ws)
    
    fetched = await store.get_workspace(ws.id)
    assert fetched is not None
    assert fetched.name == "Test"


@pytest.mark.asyncio
async def test_list_workspaces(store):
    """测试列出工作区"""
    await store.create_workspace(Workspace(name="WS1"))
    await store.create_workspace(Workspace(name="WS2"))
    
    workspaces = await store.list_workspaces()
    assert len(workspaces) >= 2


@pytest.mark.asyncio
async def test_create_insight(store):
    """测试创建洞察"""
    ws = await store.create_workspace(Workspace(name="Test"))
    
    insight = Insight(
        workspace_id=ws.id,
        type="competitor_move",
        title="竞品A降价20%",
        summary="竞品A在定价页面将基础版价格从$99降至$79",
        severity=InsightSeverity.HIGH,
        confidence=0.85,
    )
    insight = await store.create_insight(insight)
    assert insight.id is not None
    assert insight.status == InsightStatus.NEW


@pytest.mark.asyncio
async def test_list_insights(store):
    """测试列出洞察"""
    ws = await store.create_workspace(Workspace(name="Test"))
    
    for i in range(3):
        await store.create_insight(Insight(
            workspace_id=ws.id,
            type="test",
            title=f"Insight {i}",
            summary="Test",
        ))
    
    insights = await store.list_insights(ws.id)
    assert len(insights) >= 3


@pytest.mark.asyncio
async def test_update_insight_status(store):
    """测试更新洞察状态"""
    ws = await store.create_workspace(Workspace(name="Test"))
    insight = await store.create_insight(Insight(
        workspace_id=ws.id,
        type="test",
        title="Test",
        summary="Test",
    ))
    
    await store.update_insight_status(insight.id, "acknowledged")
    fetched = await store.get_insight(insight.id)
    assert fetched.status == InsightStatus.ACKNOWLEDGED
