"""Insight Flow 配额账本 + 熔断器

每个 source 实例的配额账本超阈值（金额/次数）自动暂停并告警
"""

from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path


class CircuitState(str, Enum):
    """熔断器状态"""
    CLOSED = "closed"      # 正常
    OPEN = "open"          # 熔断中
    HALF_OPEN = "half_open"  # 半开（试探）


class QuotaExceeded(Exception):
    """配额超限"""
    pass


class CircuitBreaker:
    """熔断器

    当配额超限时自动熔断，防止继续调用导致超支。
    """

    def __init__(
        self,
        source_id: str,
        max_calls: int = 1000,
        max_cost: float = 10.0,
        window_hours: int = 24,
        recovery_timeout_minutes: int = 30,
    ):
        self.source_id = source_id
        self.max_calls = max_calls
        self.max_cost = max_cost
        self.window_hours = window_hours
        self.recovery_timeout_minutes = recovery_timeout_minutes

        self.state = CircuitState.CLOSED
        self.call_count = 0
        self.total_cost = 0.0
        self.last_failure: datetime | None = None
        self.window_start = datetime.now(UTC)

    def _reset_window_if_needed(self) -> None:
        """重置窗口（如果需要）"""
        now = datetime.now(UTC)
        if now - self.window_start > timedelta(hours=self.window_hours):
            self.window_start = now
            self.call_count = 0
            self.total_cost = 0.0

    def check(self) -> bool:
        """检查是否允许调用

        Returns:
            True: 允许调用
            False: 熔断中

        Raises:
            QuotaExceeded: 配额超限
        """
        self._reset_window_if_needed()

        # 检查熔断状态
        if self.state == CircuitState.OPEN:
            # 检查是否可以尝试恢复
            if self.last_failure:
                recovery_time = self.last_failure + timedelta(minutes=self.recovery_timeout_minutes)
                if datetime.now(UTC) > recovery_time:
                    self.state = CircuitState.HALF_OPEN
                    return True
            return False

        # 检查配额
        if self.call_count >= self.max_calls:
            self._trip(f"调用次数超限: {self.call_count}/{self.max_calls}")
            raise QuotaExceeded(f"调用次数超限: {self.call_count}/{self.max_calls}")

        if self.total_cost >= self.max_cost:
            self._trip(f"费用超限: ${self.total_cost:.4f}/${self.max_cost:.4f}")
            raise QuotaExceeded(f"费用超限: ${self.total_cost:.4f}/${self.max_cost:.4f}")

        return True

    def record_call(self, cost: float = 0.0) -> None:
        """记录一次调用"""
        self._reset_window_if_needed()
        self.call_count += 1
        self.total_cost += cost

    def record_success(self) -> None:
        """记录成功（半开状态恢复）"""
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """记录失败"""
        if self.state == CircuitState.HALF_OPEN:
            self._trip("半开状态试探失败")

    def _trip(self, reason: str) -> None:
        """触发熔断"""
        self.state = CircuitState.OPEN
        self.last_failure = datetime.now(UTC)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "state": self.state.value,
            "call_count": self.call_count,
            "total_cost": self.total_cost,
            "max_calls": self.max_calls,
            "max_cost": self.max_cost,
            "window_start": self.window_start.isoformat(),
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
        }


class QuotaLedger:
    """配额账本

    追踪每个 source 的调用次数和费用。
    """

    def __init__(self, db_path: Path | None = None):
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_breaker(self, source_id: str, **kwargs) -> CircuitBreaker:
        """获取或创建熔断器"""
        if source_id not in self._breakers:
            self._breakers[source_id] = CircuitBreaker(source_id, **kwargs)
        return self._breakers[source_id]

    def check_quota(self, source_id: str) -> bool:
        """检查配额"""
        breaker = self.get_breaker(source_id)
        return breaker.check()

    def record_usage(self, source_id: str, cost: float = 0.0) -> None:
        """记录使用"""
        breaker = self.get_breaker(source_id)
        breaker.record_call(cost)

    def get_usage(self, source_id: str) -> dict:
        """获取使用情况"""
        breaker = self.get_breaker(source_id)
        return breaker.to_dict()

    def get_all_usage(self) -> dict[str, dict]:
        """获取所有 source 的使用情况"""
        return {sid: b.to_dict() for sid, b in self._breakers.items()}

    def reset_window(self, source_id: str) -> None:
        """重置窗口"""
        if source_id in self._breakers:
            breaker = self._breakers[source_id]
            breaker.window_start = datetime.now(UTC)
            breaker.call_count = 0
            breaker.total_cost = 0.0
            breaker.state = CircuitState.CLOSED


# 全局实例
_quota_ledger: QuotaLedger | None = None


def get_quota_ledger() -> QuotaLedger:
    """获取全局配额账本"""
    global _quota_ledger
    if _quota_ledger is None:
        _quota_ledger = QuotaLedger()
    return _quota_ledger
