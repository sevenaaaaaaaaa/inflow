"""测试流量诊断引擎（AARRR 映射 + 异常检测 + 洞察生成）"""

import pytest

from insflow.engine.diagnosis import DiagnosisEngine


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    """诊断引擎（数据目录与全局 store 重定向到临时目录）"""
    import insflow.core.files as files_mod
    from insflow.core.store import Store, reset_store

    monkeypatch.setattr(files_mod, "DATA_DIR", tmp_path)

    s = Store(db_path=tmp_path / "test.db")
    await s.connect()
    await s.migrate()
    reset_store(s)

    # 创建测试 workspace（洞察外键依赖）
    from insflow.core.entities import Workspace
    await s.create_workspace(Workspace(id="test-ws", name="Test WS"))

    yield DiagnosisEngine("test-ws")

    reset_store(None)
    await s.close()


class TestAARRRMapping:
    def test_map_to_aarrr(self, engine):
        metrics = {
            "gsc_clicks": [{"value": 100}],
            "ga4_sessions": [{"value": 50}],
            "ga4_conversions": [{"value": 5}],
        }
        aarrr = engine.map_to_aarrr(metrics)
        assert aarrr["acquisition"]["sources"] == ["gsc"]
        assert aarrr["activation"]["sources"] == ["ga4"]
        assert aarrr["revenue"]["metrics"] == metrics["ga4_conversions"]


class TestAnomalyDetection:
    def test_detect_drop(self, engine):
        series = [100, 102, 98, 101, 60]  # 最后一期骤降 40%
        anomalies = engine.detect_anomalies(series)
        assert len(anomalies) >= 1
        assert anomalies[-1]["kind"] == "drop"
        assert anomalies[-1]["change_pct"] < -0.2

    def test_detect_spike(self, engine):
        series = [100, 101, 99, 100, 180]  # 最后一期骤升 80%
        anomalies = engine.detect_anomalies(series)
        assert anomalies[-1]["kind"] == "spike"

    def test_no_anomaly_in_stable_series(self, engine):
        series = [100, 101, 99, 100, 102, 98]
        assert engine.detect_anomalies(series) == []

    def test_short_series(self, engine):
        assert engine.detect_anomalies([100, 90]) == []


class TestDiagnosisRun:
    @pytest.mark.asyncio
    async def test_run_creates_insights(self, engine):
        """注入带异常的数据 → 应产出流量下降洞察"""
        source_data = {
            "traffic": [
                {"value": 1000},
                {"value": 1020},
                {"value": 980},
                {"value": 1010},
                {"value": 500},  # 骤降 50%
            ],
            "conversion": [
                {"value": 0.05},
                {"value": 0.048},
                {"value": 0.052},
            ],
        }
        result = await engine.run(source_data)
        assert result["insights_created"] >= 1
        assert len(result["insight_ids"]) >= 1
        assert result["report_path"]  # 报告已落盘

    @pytest.mark.asyncio
    async def test_run_empty_data(self, engine):
        """空数据 → 不产出洞察但不报错"""
        result = await engine.run({})
        assert result["insights_created"] == 0
