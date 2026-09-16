"""Insight Flow 洞察质量门

洞察必须可执行：每个 Insight 对象强制携带 recommended_actions，
动作直接映射到 OpenFlow 自动化、MFlow 内容任务或通用 Webhook。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..core.entities import Insight, InsightAction, InsightSeverity


class GateResult(str, Enum):
    """质量门结果"""
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class GateCheck:
    """质量门检查项"""
    name: str
    result: GateResult
    message: str = ""


@dataclass
class QualityReport:
    """质量门报告"""
    passed: bool
    checks: list[GateCheck]
    errors: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [
                {"name": c.name, "result": c.result.value, "message": c.message}
                for c in self.checks
            ],
            "errors": self.errors,
            "warnings": self.warnings,
        }


class QualityGates:
    """洞察质量门

    规则：
    1. evidence_json 不能为空（必须有证据）
    2. actions_json 不能为空（必须有推荐动作）
    3. confidence 必须 >= 0.3（低置信度需人工确认）
    4. severity 为 critical/high 时必须有至少 2 条 evidence
    5. 每个 action 必须有 description
    """

    MIN_CONFIDENCE = 0.3
    HIGH_SEVERITY_MIN_EVIDENCE = 2

    def validate(self, insight: Insight) -> QualityReport:
        """验证洞察质量"""
        checks = []
        errors = []
        warnings = []

        # 检查 1: 必须有证据
        if not insight.evidence_json:
            checks.append(GateCheck(
                name="evidence_required",
                result=GateResult.FAIL,
                message="洞察必须包含至少一条证据 (evidence_json)",
            ))
            errors.append("evidence_json 不能为空")
        else:
            checks.append(GateCheck(
                name="evidence_required",
                result=GateResult.PASS,
                message=f"包含 {len(insight.evidence_json)} 条证据",
            ))

        # 检查 2: 必须有推荐动作
        if not insight.actions_json:
            checks.append(GateCheck(
                name="actions_required",
                result=GateResult.FAIL,
                message="洞察必须包含至少一个推荐动作 (actions_json)",
            ))
            errors.append("actions_json 不能为空")
        else:
            checks.append(GateCheck(
                name="actions_required",
                result=GateResult.PASS,
                message=f"包含 {len(insight.actions_json)} 个推荐动作",
            ))

        # 检查 3: 置信度阈值
        if insight.confidence < self.MIN_CONFIDENCE:
            checks.append(GateCheck(
                name="confidence_threshold",
                result=GateResult.WARN,
                message=f"置信度 {insight.confidence:.0%} 低于阈值 {self.MIN_CONFIDENCE:.0%}，建议人工确认",
            ))
            warnings.append(f"置信度过低: {insight.confidence:.0%}")
        else:
            checks.append(GateCheck(
                name="confidence_threshold",
                result=GateResult.PASS,
                message=f"置信度 {insight.confidence:.0%}",
            ))

        # 检查 4: 高严重程度需更多证据
        if insight.severity in (InsightSeverity.CRITICAL, InsightSeverity.HIGH):
            if len(insight.evidence_json) < self.HIGH_SEVERITY_MIN_EVIDENCE:
                checks.append(GateCheck(
                    name="high_severity_evidence",
                    result=GateResult.WARN,
                    message=f"{insight.severity.value} 级洞察建议至少 {self.HIGH_SEVERITY_MIN_EVIDENCE} 条证据",
                ))
                warnings.append("高严重程度洞察证据不足")
            else:
                checks.append(GateCheck(
                    name="high_severity_evidence",
                    result=GateResult.PASS,
                    message="高严重程度洞察证据充足",
                ))

        # 检查 5: action 必须有描述
        def action_desc(a) -> str:
            if isinstance(a, dict):
                return a.get("description", "")
            return getattr(a, "description", "")

        actions_without_desc = [
            a for a in insight.actions_json
            if not action_desc(a)
        ]
        if actions_without_desc:
            checks.append(GateCheck(
                name="action_description",
                result=GateResult.WARN,
                message=f"{len(actions_without_desc)} 个动作缺少描述",
            ))
            warnings.append("部分动作缺少描述")
        else:
            checks.append(GateCheck(
                name="action_description",
                result=GateResult.PASS,
                message="所有动作都有描述",
            ))

        # 总结
        has_fail = any(c.result == GateResult.FAIL for c in checks)
        passed = not has_fail

        return QualityReport(
            passed=passed,
            checks=checks,
            errors=errors,
            warnings=warnings,
        )

    def filter_insights(self, insights: list[Insight]) -> tuple[list[Insight], list[Insight]]:
        """过滤洞察：返回 (通过, 未通过)"""
        passed = []
        failed = []

        for insight in insights:
            report = self.validate(insight)
            if report.passed:
                passed.append(insight)
            else:
                failed.append(insight)

        return passed, failed


# 全局实例
_quality_gates: Optional[QualityGates] = None


def get_quality_gates() -> QualityGates:
    """获取全局质量门实例"""
    global _quality_gates
    if _quality_gates is None:
        _quality_gates = QualityGates()
    return _quality_gates
