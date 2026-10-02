"""E3 开关矩阵测试：预设展开、单开关覆盖、非法值拒绝、快照指纹。

验收对应："切换配置预设可观察到行为差异"的最小 demo（见 test_preset_switching_changes_behavior_demo）。
"""
import pytest
from pydantic import ValidationError

from app.core.config import ABLATION_PRESETS, Settings

# 测试隔离：屏蔽宿主环境里可能存在的开关变量，保证断言确定性
_SWITCH_ENV = ("PRESET", "CONTEXT_MODE", "WRITE_MODE", "RETRIEVAL_MODE",
               "SUPERSEDE_ENABLED", "EVICT_ENABLED", "GATE_ENABLED")


@pytest.fixture(autouse=True)
def _clean_switch_env(monkeypatch):
    for key in _SWITCH_ENV:
        monkeypatch.delenv(key, raising=False)


def make_settings(**overrides) -> Settings:
    """独立构造 Settings：不读 .env（_env_file=None），只吃默认值 + 显式入参。"""
    return Settings(_env_file=None, **overrides)


def test_all_presets_defined():
    assert set(ABLATION_PRESETS) == {"B1", "B2", "B3", "B4P", "B4", "B5"}


def test_default_is_b5_full_system():
    s = make_settings()
    assert s.PRESET == "B5"
    assert (s.CONTEXT_MODE, s.WRITE_MODE, s.RETRIEVAL_MODE) == ("window+memory", "cards", "full")
    assert s.SUPERSEDE_ENABLED and s.EVICT_ENABLED and s.GATE_ENABLED
    assert s.memory_read_enabled and s.memory_write_enabled


def test_b1_full_history_no_write_no_read():
    s = make_settings(PRESET="B1")
    assert s.CONTEXT_MODE == "full_history"
    assert not s.memory_write_enabled and not s.memory_read_enabled


def test_b2_pure_window_baseline():
    s = make_settings(PRESET="B2")
    assert (s.CONTEXT_MODE, s.WRITE_MODE, s.RETRIEVAL_MODE) == ("window", "off", "off")
    assert not (s.SUPERSEDE_ENABLED or s.EVICT_ENABLED or s.GATE_ENABLED)


def test_b3_store_without_use():
    s = make_settings(PRESET="B3")
    assert s.memory_write_enabled and not s.memory_read_enabled  # 存了不用
    assert s.RETRIEVAL_MODE == "off" and s.GATE_ENABLED is False


def test_b4p_naive_rag_raw_chunks():
    s = make_settings(PRESET="B4P")
    assert (s.WRITE_MODE, s.RETRIEVAL_MODE) == ("raw_chunks", "flat_raw")
    assert s.memory_read_enabled and not s.SUPERSEDE_ENABLED


def test_b4_prime_alias_normalized():
    assert make_settings(PRESET="B4′").PRESET == "B4P"


def test_b4_flat_no_governance():
    s = make_settings(PRESET="B4")
    assert (s.WRITE_MODE, s.RETRIEVAL_MODE) == ("cards", "flat")
    assert not (s.SUPERSEDE_ENABLED or s.EVICT_ENABLED or s.GATE_ENABLED)


def test_explicit_switch_overrides_preset(monkeypatch):
    """H2/H3 单因子实验：预设切档 + 只覆盖一个开关，其余照常展开。"""
    monkeypatch.setenv("PRESET", "B5")
    monkeypatch.setenv("GATE_ENABLED", "False")
    s = Settings(_env_file=None)
    assert s.CONTEXT_MODE == "window+memory" and s.RETRIEVAL_MODE == "full"
    assert s.GATE_ENABLED is False


def test_invalid_preset_rejected():
    with pytest.raises(ValidationError):
        make_settings(PRESET="B6")


def test_invalid_mode_rejected():
    with pytest.raises(ValidationError):
        make_settings(CONTEXT_MODE="everything")


def test_snapshot_hash_stable_and_sensitive():
    s1, s2, s3 = make_settings(), make_settings(), make_settings(PRESET="B2")
    assert s1.snapshot_hash() == s2.snapshot_hash()  # 同条件同哈希
    assert s1.snapshot_hash() != s3.snapshot_hash()  # 换档位换哈希
    snap = s1.experiment_snapshot()
    assert snap["preset"] == "B5" and snap["model"] == s1.LLM_MODEL


def test_preset_switching_changes_behavior_demo():
    """最小 demo（E3 验收）：同一段"行为探针"，六个档位切出可观察差异。"""
    def probe(s: Settings) -> str:
        ctx = "记忆注入" if s.CONTEXT_MODE == "window+memory" else s.CONTEXT_MODE
        act = "读" if s.memory_read_enabled else "不读"
        return f"{ctx}/{act}"

    table = [(make_settings(PRESET=p), expect) for p, expect in (
        ("B1", "full_history/不读"),
        ("B2", "window/不读"),
        ("B3", "window/不读"),
        ("B4P", "记忆注入/读"),
        ("B4", "记忆注入/读"),
        ("B5", "记忆注入/读"),
    )]
    results = {s.PRESET: probe(s) for s, _ in table}
    assert results["B2"] != results["B5"]  # 行为差异存在
    for s, expect in table:
        assert probe(s) == expect
