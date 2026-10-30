"""E3 埋点测试：JSONL 按天落盘、字段齐全、B2 空集照记、fail-open、目录解析。"""
import json
from datetime import date
from pathlib import Path

import pytest

from app.core import telemetry
from app.core.config import settings


@pytest.fixture
def tele_dir(tmp_path, monkeypatch):
    """把埋点目录指到临时目录，并确保埋点开关为开。"""
    monkeypatch.setattr(settings, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(settings, "TELEMETRY_DIR", str(tmp_path / "telemetry"))
    return tmp_path / "telemetry"


def _today_file(tele_dir: Path) -> Path:
    return tele_dir / f"telemetry-{date.today():%Y%m%d}.jsonl"


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_emit_writes_daily_jsonl(tele_dir):
    ok = telemetry.emit_trace(
        trace_id="t-001", session_id="s1",
        chain={"triage": "fault", "hit": False, "fault": True},
        cost={"stage_ms": {"retrieval": 12.5}, "tokens": {"total": 100}},
        health={"injected_count": 2},
    )
    assert ok is True
    path = _today_file(tele_dir)
    assert path.exists()
    (rec,) = _read_lines(path)
    assert rec["schema_version"] == telemetry.SCHEMA_VERSION
    assert rec["trace_id"] == "t-001" and rec["session_id"] == "s1"
    assert rec["chain"]["fault"] is True
    assert rec["cost"]["tokens"]["total"] == 100
    assert rec["health"]["injected_count"] == 2


def test_empty_record_still_written(tele_dir):
    """没有召回的一轮：候选/注入为空集，也必须落盘（可观测性不能有空洞）。"""
    ok = telemetry.emit_trace(trace_id="t-empty", chain={"triage": "skip", "injected": []},
                              health={"injected_count": 0})
    assert ok is True
    (rec,) = _read_lines(_today_file(tele_dir))
    assert rec["chain"]["injected"] == []
    assert rec["health"]["injected_count"] == 0


def test_appends_multiple_records(tele_dir):
    telemetry.emit_trace(trace_id="t-a")
    telemetry.emit_trace(trace_id="t-b")
    lines = _read_lines(_today_file(tele_dir))
    assert [r["trace_id"] for r in lines] == ["t-a", "t-b"]


def test_disabled_returns_false_without_file(tele_dir, monkeypatch):
    monkeypatch.setattr(settings, "TELEMETRY_ENABLED", False)
    assert telemetry.emit_trace(trace_id="t-off") is False
    assert not tele_dir.exists()


def test_fail_open_on_write_error(tele_dir, monkeypatch):
    """写盘炸了也不许往外抛异常（埋点可以丢一条，对话不能挂一次）。"""
    def boom(path: str, line: str) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(telemetry, "_append_line", boom)
    assert telemetry.emit_trace(trace_id="t-boom") is False  # 不抛异常


def test_current_log_path_uses_given_date(tele_dir):
    p = telemetry.current_log_path(date(2026, 1, 1))
    assert p.endswith("telemetry-20260101.jsonl")


def test_relative_dir_resolves_under_base_dir(monkeypatch):
    monkeypatch.setattr(settings, "TELEMETRY_DIR", "data/telemetry")
    assert Path(telemetry.resolve_telemetry_dir()) == Path(settings.BASE_DIR) / "data/telemetry"
