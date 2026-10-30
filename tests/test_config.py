"""配置测试：必填项、默认值、环境变量覆盖、非法值拒绝。"""
import os

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make_settings(**overrides) -> Settings:
    """独立构造 Settings：不读 .env（_env_file=None），只吃默认值 + 显式入参。"""
    return Settings(_env_file=None, **overrides)


def test_required_fields_missing(monkeypatch):
    """LLM_API_KEY / DATABASE_URL 没有默认值——缺了就启动失败，不带病运行。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_defaults():
    """主干参数默认值（改这里 = 改系统行为，须同步 .env.example）。"""
    s = make_settings(LLM_API_KEY="k", DATABASE_URL="sqlite+aiosqlite:///./x.db")
    assert s.WINDOW_SIZE == 10
    assert s.GATE_THRESHOLD == 0.35
    assert s.INJECT_MAX_CARDS == 3
    assert s.SOURCE_SCAN_LIMIT == 200
    assert s.LINE_PREFETCH_LIMIT == 3
    assert s.EXTRACT_WATERMARK_ROUNDS == 4
    assert s.EXTRACT_IDLE_TIMEOUT_MINUTES == 30
    assert s.EVICT_GOAL_GRACE_DAYS == 30
    assert s.EVICT_PREF_HALFLIFE_DAYS == 90
    assert s.EVICT_MIN_SCORE == 0.25
    assert s.GATE_MODEL is None and s.GATE_UPGRADE_MODEL is None
    assert s.EXTRACT_MODEL is None


def test_env_override(monkeypatch):
    """环境变量覆盖默认值（.env 与之同源，pydantic-settings 统一处理）。"""
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./env.db")
    monkeypatch.setenv("WINDOW_SIZE", "20")
    monkeypatch.setenv("GATE_THRESHOLD", "0.5")
    s = Settings(_env_file=None)
    assert s.LLM_API_KEY == "env-key"
    assert s.WINDOW_SIZE == 20
    assert s.GATE_THRESHOLD == 0.5


def test_invalid_type_rejected():
    with pytest.raises(ValidationError):
        make_settings(LLM_API_KEY="k", DATABASE_URL="sqlite://", WINDOW_SIZE="ten")


def test_base_dir_points_to_project_root():
    s = make_settings(LLM_API_KEY="k", DATABASE_URL="sqlite://")
    assert os.path.isdir(os.path.join(s.BASE_DIR, "app"))
    assert os.path.isfile(os.path.join(s.BASE_DIR, "pytest.ini"))
