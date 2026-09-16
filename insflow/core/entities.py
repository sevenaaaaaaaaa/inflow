"""Insight Flow 核心实体模型定义"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class WorkspaceStage(str, Enum):
    """工作区增长阶段"""
    S0 = "S0"  # 冷启动
    S1 = "S1"  # 增长获客
    S2 = "S2"  # 全生命周期运营
    S3 = "S3"  # 商业化优化


class MaturityLevel(str, Enum):
    """数据成熟度等级"""
    L0 = "L0"  # 无数据
    L1 = "L1"  # 基础数据
    L2 = "L2"  # 进阶数据
    L3 = "L3"  # 完整数据
    L4 = "L4"  # 智能数据


class SourceStatus(str, Enum):
    """数据源状态"""
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    QUOTA_EXCEEDED = "quota_exceeded"


class MonitorState(str, Enum):
    """监控任务状态"""
    IDLE = "idle"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class InsightStatus(str, Enum):
    """洞察状态"""
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    ACTIONED = "actioned"
    VERIFIED = "verified"
    DISMISSED = "dismissed"


class InsightSeverity(str, Enum):
    """洞察严重程度"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ActionState(str, Enum):
    """动作状态"""
    PENDING = "pending"
    DISPATCHED = "dispatched"
    DONE = "done"
    FAILED = "failed"
    VERIFYING = "verifying"
    VERIFIED = "verified"


class ActionVerdict(str, Enum):
    """动作验证结论"""
    EFFECTIVE = "effective"
    NEUTRAL = "neutral"
    HARMFUL = "harmful"


class Workspace(BaseModel):
    """工作区"""
    id: str = Field(default_factory=lambda: "")
    name: str
    stage: WorkspaceStage = WorkspaceStage.S0
    maturity_level: MaturityLevel = MaturityLevel.L0
    settings_json: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Source(BaseModel):
    """数据源实例"""
    id: str = Field(default_factory=lambda: "")
    workspace_id: str
    plugin_id: str
    config_enc: dict = Field(default_factory=dict)  # 加密后的配置
    quota_ledger_json: dict = Field(default_factory=dict)
    status: SourceStatus = SourceStatus.ACTIVE
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Monitor(BaseModel):
    """监控任务"""
    id: str = Field(default_factory=lambda: "")
    workspace_id: str
    kind: str  # keyword | competitor_site | brand_mention | journey
    target_json: dict = Field(default_factory=dict)
    schedule_cron: str = "0 */6 * * *"  # 默认每6小时
    last_run_at: Optional[datetime] = None
    state: MonitorState = MonitorState.IDLE
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RawRecord(BaseModel):
    """原始采集记录"""
    id: str = Field(default_factory=lambda: "")
    workspace_id: str
    source_id: str
    monitor_id: str
    kind: str
    payload_json: dict = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=datetime.utcnow)


class Metric(BaseModel):
    """归一化指标时序"""
    id: str = Field(default_factory=lambda: "")
    workspace_id: str
    entity_type: str  # site | keyword | competitor | topic
    entity_id: str
    metric: str
    value: float
    dim_json: dict = Field(default_factory=dict)
    ts: datetime = Field(default_factory=datetime.utcnow)


class InsightAction(BaseModel):
    """洞察推荐动作"""
    action_type: str  # mflow.create_content | openflow.automation | webhook
    target_ref: Optional[str] = None
    params_json: dict = Field(default_factory=dict)
    description: str = ""


class Insight(BaseModel):
    """洞察对象"""
    id: str = Field(default_factory=lambda: "")
    workspace_id: str
    type: str  # competitor_move | traffic_anomaly | keyword_opportunity | ...
    title: str
    summary: str
    severity: InsightSeverity = InsightSeverity.MEDIUM
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    evidence_json: list[dict] = Field(default_factory=list)
    models_json: list[str] = Field(default_factory=list)  # 产出此洞察的模型ID
    actions_json: list[InsightAction] = Field(default_factory=list)
    stage_tags_json: list[str] = Field(default_factory=list)
    status: InsightStatus = InsightStatus.NEW
    created_at: datetime = Field(default_factory=datetime.utcnow)
    verified_at: Optional[datetime] = None


class Action(BaseModel):
    """执行动作"""
    id: str = Field(default_factory=lambda: "")
    workspace_id: str
    insight_id: str
    action_type: str
    target_ref: Optional[str] = None
    params_json: dict = Field(default_factory=dict)
    state: ActionState = ActionState.PENDING
    dispatched_at: Optional[datetime] = None
    result_json: dict = Field(default_factory=dict)
    verify_window_until: Optional[datetime] = None
    baseline_json: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Feedback(BaseModel):
    """动作反馈"""
    id: str = Field(default_factory=lambda: "")
    workspace_id: str
    action_id: str
    metric: str
    before: float
    after: float
    delta: float
    verdict: ActionVerdict
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)
