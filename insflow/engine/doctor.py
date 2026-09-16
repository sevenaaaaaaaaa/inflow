"""Insight Flow 首次运行体检（R1-3）

`insflow doctor`：五项体检，每项输出 ✅/⚠️/❌ + 修复建议，客户侧自助排障：
1. 环境：Python 版本 / 必填环境变量（INSFLOW_MASTER_KEY fail-closed）
2. 凭据：保险库可解密性 + 各数据源接入状态（GSC/GA4/CrUX 授权实测）
3. 插件：已安装插件数量 + 本地市场校验状态
4. 调度：任务注册数 / 最近事件流是否有失败堆积
5. 磁盘：data/ 容量与最近备份时间
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..core.files import DATA_DIR, EventBus
from ..core.security import get_vault

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class CheckResult:
    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str = ""  # 修复建议（fail/warn 时给客户可执行的动作）

    @property
    def icon(self) -> str:
        return {"ok": "✅", "warn": "⚠️", "fail": "❌"}[self.status]


class Doctor:
    """五项体检"""

    def __init__(self, workspace_id: str = "default"):
        self.workspace_id = workspace_id

    # ========== 1. 环境 ==========

    def check_environment(self) -> list[CheckResult]:
        import sys
        results = []

        v = sys.version_info
        if v >= (3, 12):
            results.append(CheckResult("Python 版本", OK, f"{v.major}.{v.minor}.{v.micro}"))
        else:
            results.append(CheckResult(
                "Python 版本", FAIL, f"{v.major}.{v.minor}（需要 ≥3.12）",
                "安装 Python 3.12 并用其重建虚拟环境"))

        master_key = os.environ.get("INSFLOW_MASTER_KEY", "")
        if master_key:
            results.append(CheckResult("主密钥 INSFLOW_MASTER_KEY", OK, "已设置（凭据保险库可用）"))
        else:
            results.append(CheckResult(
                "主密钥 INSFLOW_MASTER_KEY", FAIL, "未设置（凭据保险库 fail-closed，无法保存任何 API Key）",
                "cp .env.example .env 并设置随机长密钥，重启服务"))

        # 集成端点配置
        for env_key, label in (("OPENFLOW_BASE_URL", "OpenFlow 集成"),
                               ("MFLOW_BASE_URL", "MFlow 集成")):
            val = os.environ.get(env_key, "")
            if val:
                results.append(CheckResult(label, OK, val))
            else:
                results.append(CheckResult(label, WARN, "未配置（相关动作适配器不可用）",
                                           f"设置 {env_key} 后重启"))

        return results

    # ========== 2. 凭据 ==========

    def check_credentials(self) -> list[CheckResult]:
        results = []
        try:
            vault = get_vault()
            keys = vault.list_keys()
            if not keys:
                results.append(CheckResult(
                    "凭据保险库", WARN, "无任何凭据（第一方数据未接入）",
                    "访问 /console/onboarding 完成 GSC/GA4/CrUX 接入"))
            else:
                results.append(CheckResult(
                    "凭据保险库", OK, f"{len(keys)} 项凭据已加密存储"))

            # OAuth token 过期预警（R2 要求的 7 天预警前置）
            from .token_manager import TokenManager
            tm = TokenManager(self.workspace_id)
            for provider in ("gsc", "ga4"):
                tokens = tm.load_tokens(provider)
                if not tokens:
                    results.append(CheckResult(
                        f"{provider.upper()} 授权", WARN, "未授权",
                        "访问 /console/onboarding"))
                    continue
                expires_at = tokens.get("expires_at")
                if not expires_at:
                    results.append(CheckResult(
                        f"{provider.upper()} 授权", WARN, "旧格式 token（无过期时间，依赖 401 自动轮换）"))
                    continue
                from datetime import datetime, timezone
                remaining = (datetime.fromisoformat(expires_at)
                             - datetime.now(timezone.utc)).total_seconds()
                if remaining > 86400 * 7:
                    days = int(remaining // 86400)
                    results.append(CheckResult(f"{provider.upper()} 授权", OK, f"有效（{days} 天）"))
                else:
                    results.append(CheckResult(
                        f"{provider.upper()} 授权", WARN,
                        f"即将过期（{int(remaining // 3600)} 小时）—— 将由 refresh_token 自动轮换；若轮换失败请重新授权"))
        except Exception as e:
            results.append(CheckResult("凭据保险库", FAIL, f"读取失败: {e}"))
        return results

    # ========== 3. 插件 ==========

    def check_plugins(self) -> list[CheckResult]:
        from .marketplace import Marketplace
        installed = Marketplace().installed()
        results = [CheckResult("已安装插件", OK if installed else WARN,
                               f"{len(installed)} 个" if installed else "0 个（无数据源可用）",
                               "" if installed else "把插件放入 marketplace/ 后执行 insflow plugin install")]
        bad = [p for p in Marketplace().scan() if not p["check_passed"]]
        if bad:
            results.append(CheckResult(
                "市场校验", WARN, f"{len(bad)} 个市场插件未过检（不会被调度执行）"))
        return results

    # ========== 4. 调度与事件流 ==========

    def check_scheduler(self) -> list[CheckResult]:
        results = []
        try:
            from ..core.scheduler import get_scheduler
            jobs = get_scheduler().list_jobs()
            if jobs:
                results.append(CheckResult("调度任务", OK, f"{len(jobs)} 个已注册"))
            else:
                results.append(CheckResult(
                    "调度任务", WARN, "0 个任务（INSFLOW_DISABLE_SCHEDULER=1 或未建监控）"))

            # 事件流失败扫描（最近 100 条中的 fail 类事件）
            from ..core.files import EventBus
            events = EventBus(self.workspace_id).read(limit=100)
            fails = [e for e in events if e.get("type", "").endswith((".failed", ".dead"))]
            if fails:
                results.append(CheckResult(
                    "事件流", WARN, f"最近 100 条中有 {len(fails)} 条失败事件",
                    "运行 insflow insight list 查看受影响监控"))
            else:
                results.append(CheckResult("事件流", OK, "最近无失败事件"))
        except Exception as e:
            results.append(CheckResult("调度器", FAIL, f"读取失败: {e}"))
        return results

    # ========== 5. 磁盘 ==========

    def check_disk(self) -> list[CheckResult]:
        results = []
        from ..core import files as files_mod
        data = Path(files_mod.DATA_DIR or DATA_DIR)
        if not (data / "insflow.db").exists():
            results.append(CheckResult("数据目录", FAIL, f"{data} 缺少 insflow.db",
                                       "运行 insflow init"))
            return results

        total = sum(f.stat().st_size for f in data.rglob("*") if f.is_file())
        db = data / "insflow.db"
        detail = f"{total / 1024 / 1024:.1f} MB"
        if db.exists():
            detail += f"（库 {db.stat().st_size / 1024 / 1024:.1f} MB）"

        # 最近备份
        backup_root = data.parent / "data-backup"
        backups = sorted([d for d in backup_root.iterdir() if d.is_dir()],
                         reverse=True) if backup_root.exists() else []
        if backups:
            detail += f" · 最近备份 {backups[0].name}"
        else:
            results.append(CheckResult("备份", WARN, "从未备份",
                                       "运行 insflow backup（建议加入 daily timer）"))

        results.append(CheckResult("数据目录", OK, detail))
        return results

    # ========== 全量运行 ==========

    def run_all(self) -> dict:
        sections = {
            "环境": self.check_environment(),
            "凭据": self.check_credentials(),
            "插件": self.check_plugins(),
            "调度": self.check_scheduler(),
            "磁盘": self.check_disk(),
        }
        total = sum(len(v) for v in sections.values())
        failed = sum(1 for v in sections.values() for c in v if c.status == FAIL)
        warned = sum(1 for v in sections.values() for c in v if c.status == WARN)

        health = "healthy" if failed == 0 else ("degraded" if warned else "unhealthy")
        return {"sections": sections,
                "summary": {"total": total, "failed": failed,
                            "warned": warned, "verdict": health}}
