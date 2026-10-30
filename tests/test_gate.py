"""判定器单测（E7）：五方向 fail-safe / 降级链 / 确定性前置 / 三层校验 / 分档 / 旁路。

验收对应（工程路线 E7）："降级路径测试全绿；五个 API 有 fail-safe 单测"。
策略：全部零真实调用——`_call_model` 用 AsyncMock 替换（分档行为直测）；
约束模式参数与 json_mode 重试降级用假 client 验证；确定性前置为纯正则，真跑。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.long_term_memory import gate

# —— 样例文本：不命中任何前置模式（保证走模型档），用于分档行为测试 ——
_NEUTRAL_Q = "今天天气怎么样"


def _ok(decision: str, confidence: float = 0.95, reason: str = "理由") -> str:
    """合法模型输出（三层校验可通过）。"""
    return json.dumps({"decision": decision, "confidence": confidence, "reason": reason},
                      ensure_ascii=False)


def _resp(content: str) -> SimpleNamespace:
    """假 OpenAI response（只用到 .choices[0].message.content）。"""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _fake_client(create) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch):
    """隔离宿主环境：判定器相关配置回到默认值（tests 后自动还原）。"""
    monkeypatch.setattr(settings, "GATE_MODEL", None)
    monkeypatch.setattr(settings, "GATE_UPGRADE_MODEL", None)
    monkeypatch.setattr(settings, "GATE_CONFIDENCE_MIN", 0.5)
    monkeypatch.setattr(settings, "GATE_TIMEOUT_SECONDS", 6.0)


@pytest.fixture
def model_mock(monkeypatch):
    """替换 _call_model（返回值为假模型原始输出；异常/序列由用例设定）。"""
    m = AsyncMock()
    monkeypatch.setattr(gate, "_call_model", m)
    return m


# ================================
# 五方向 fail-safe（判定器挂了系统照转）
# ================================

@pytest.mark.asyncio
async def test_page_fault_failsafe_yes(model_mock):
    model_mock.side_effect = RuntimeError("boom")
    r = await gate.judge_page_fault(_NEUTRAL_Q)
    assert (r.decision, r.origin) == ("yes", gate.ORIGIN_FAILSAFE)  # 宁多回忆
    assert model_mock.await_count == 2                              # 1 次 + 重试 1 次
    assert "兜底" in r.reason and r.confidence == 0.0


@pytest.mark.asyncio
async def test_supersede_failsafe_coexist(model_mock):
    model_mock.side_effect = RuntimeError("boom")
    r = await gate.judge_supersede("用户住在上海", "用户搬到了北京")
    assert (r.decision, r.origin) == ("coexist", gate.ORIGIN_FAILSAFE)  # 宁并存


@pytest.mark.asyncio
async def test_backfill_failsafe_no(model_mock):
    model_mock.side_effect = RuntimeError("boom")
    r = await gate.judge_backfill("事件A", "事件B")
    assert (r.decision, r.origin) == ("no", gate.ORIGIN_FAILSAFE)  # 宁不连线


@pytest.mark.asyncio
async def test_shortlist_failsafe_low(model_mock):
    model_mock.side_effect = RuntimeError("boom")
    r = await gate.judge_shortlist("我周末去哪露营好？", "用户买过一顶帐篷")
    assert (r.decision, r.origin) == ("low", gate.ORIGIN_FAILSAFE)  # 宁不注入


@pytest.mark.asyncio
async def test_ownership_archive_without_model_call(model_mock):
    r = await gate.judge_ownership("明天提醒我开会")
    assert (r.decision, r.origin) == ("archive", gate.ORIGIN_FAILSAFE)  # V1 恒归档案
    assert model_mock.await_count == 0


# ================================
# 正常路径与三层校验
# ================================

@pytest.mark.asyncio
async def test_model_decision_adopted(model_mock):
    model_mock.return_value = _ok("no", 0.92)
    r = await gate.judge_page_fault(_NEUTRAL_Q)
    assert (r.decision, r.confidence, r.origin) == ("no", 0.92, gate.ORIGIN_MODEL)
    assert model_mock.await_count == 1


@pytest.mark.asyncio
async def test_fenced_json_accepted(model_mock):
    model_mock.return_value = "```json\n" + _ok("yes") + "\n```"
    r = await gate.judge_backfill("事件A", "事件B")
    assert r.origin == gate.ORIGIN_MODEL and r.decision == "yes"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "not a json",                                                      # 格式层
    '[1, 2, 3]',                                                       # 格式层：非对象
    '{"confidence": 0.9, "reason": "x"}',                              # schema：缺字段
    '{"decision": "maybe", "confidence": 0.9, "reason": "x"}',         # schema：枚举非法
    '{"decision": "no", "confidence": 1.7, "reason": "x"}',            # 语义：置信越界
    '{"decision": "no", "confidence": 0.9, "reason": " "}',            # 语义：理由为空
])
async def test_bad_output_falls_to_failsafe(model_mock, bad):
    model_mock.return_value = bad
    r = await gate.judge_backfill("事件A", "事件B")
    assert (r.decision, r.origin) == ("no", gate.ORIGIN_FAILSAFE)
    assert model_mock.await_count == 2                                 # 重试后仍坏 → 兜底


@pytest.mark.asyncio
async def test_retry_recovers_and_uses_pure_prompt(model_mock):
    model_mock.side_effect = ["bad json", _ok("supersede", 0.9)]
    r = await gate.judge_supersede("用户住在上海", "用户搬到了北京")
    assert (r.decision, r.origin) == ("supersede", gate.ORIGIN_MODEL)
    assert model_mock.await_count == 2
    assert model_mock.await_args.kwargs["json_mode"] is False          # 重试 = 纯 prompt 模式


# ================================
# 约束模式参数（假 client，真过 _call_model）
# ================================

@pytest.mark.asyncio
async def test_constraint_mode_kwargs(monkeypatch):
    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return _resp(_ok("yes"))

    monkeypatch.setattr(gate, "client", _fake_client(fake_create))
    r = await gate.judge_backfill("事件A", "事件B")
    assert r.origin == gate.ORIGIN_MODEL
    kw = calls[0]
    assert kw["model"] == (settings.GATE_MODEL or settings.LLM_MODEL)
    assert kw["temperature"] == 0.0 and kw["top_p"] == 0.1
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["timeout"] == settings.GATE_TIMEOUT_SECONDS
    assert [m["role"] for m in kw["messages"]] == ["system", "user"]


@pytest.mark.asyncio
async def test_json_mode_failure_retries_as_pure_prompt(monkeypatch):
    """端点不支持 response_format（400 类错误）时，重试自动降级为纯 prompt 模式。"""
    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("response_format unsupported")
        return _resp(_ok("yes"))

    monkeypatch.setattr(gate, "client", _fake_client(fake_create))
    r = await gate.judge_backfill("事件A", "事件B")
    assert (r.decision, r.origin) == ("yes", gate.ORIGIN_MODEL)
    assert "response_format" in calls[0] and "response_format" not in calls[1]


# ================================
# 低置信：不采信，按保守方向
# ================================

@pytest.mark.asyncio
async def test_low_confidence_flips_to_conservative(model_mock):
    model_mock.return_value = _ok("no", 0.3)                           # 模型说"不回源"但没把握
    r = await gate.judge_page_fault(_NEUTRAL_Q)
    assert (r.decision, r.origin) == ("yes", gate.ORIGIN_FAILSAFE)
    assert r.confidence == 0.3 and "低置信" in r.reason


@pytest.mark.asyncio
async def test_low_confidence_same_direction_is_harmless(model_mock):
    model_mock.return_value = _ok("yes", 0.2)                          # 与保守方向同向
    r = await gate.judge_page_fault(_NEUTRAL_Q)
    assert (r.decision, r.origin) == ("yes", gate.ORIGIN_FAILSAFE)


# ================================
# 升级档（可选，D19）
# ================================

@pytest.mark.asyncio
async def test_upgrade_model_used_after_two_failures(model_mock, monkeypatch):
    monkeypatch.setattr(settings, "GATE_UPGRADE_MODEL", "deepseek-chat")
    model_mock.side_effect = ["bad1", "bad2", _ok("no", 0.9)]
    r = await gate.judge_backfill("事件A", "事件B")
    assert (r.decision, r.origin) == ("no", gate.ORIGIN_UPGRADE)
    assert model_mock.await_count == 3
    assert model_mock.await_args.args[0] == "deepseek-chat"            # 第三次换升级档模型


@pytest.mark.asyncio
async def test_upgrade_failure_goes_failsafe(model_mock, monkeypatch):
    monkeypatch.setattr(settings, "GATE_UPGRADE_MODEL", "deepseek-chat")
    model_mock.side_effect = ["bad1", "bad2", "bad3"]
    r = await gate.judge_backfill("事件A", "事件B")
    assert (r.decision, r.origin) == ("no", gate.ORIGIN_FAILSAFE)
    assert model_mock.await_count == 3


# ================================
# 确定性前置（少而准；仅缺页；宁漏勿误）
# ================================

@pytest.mark.asyncio
async def test_prefilter_hook_shortcut_yes(model_mock):
    for q in ("你还记得我上次说的搬家计划吗？",
              "我之前说过我喜欢吃辣",
              "咱们上次聊的那家餐厅叫什么",
              "我们说好了周末去露营"):
        r = await gate.judge_page_fault(q)
        assert (r.decision, r.origin) == ("yes", gate.ORIGIN_PREFILTER), q
        assert r.confidence == 1.0 and r.latency_ms == 0.0
    assert model_mock.await_count == 0


@pytest.mark.asyncio
async def test_prefilter_smalltalk_shortcut_no(model_mock):
    for q in ("你好", "你好！", "谢谢啦", "嗯嗯", "哈哈哈", "hello", "好的"):
        r = await gate.judge_page_fault(q)
        assert (r.decision, r.origin) == ("no", gate.ORIGIN_PREFILTER), q
    assert model_mock.await_count == 0


@pytest.mark.asyncio
async def test_prefilter_hook_beats_smalltalk(model_mock):
    r = await gate.judge_page_fault("你好，还记得上次那家餐厅吗？")
    assert (r.decision, r.origin) == ("yes", gate.ORIGIN_PREFILTER)
    assert model_mock.await_count == 0


@pytest.mark.asyncio
async def test_prefilter_not_greedy(model_mock):
    """非高置信模式不短路（宁漏勿误）：含"好"的普通句、形近时间词均交模型。"""
    model_mock.return_value = _ok("yes")
    r = await gate.judge_page_fault("我上个星期去了上海")              # "上个"≠"上次"
    assert (r.decision, r.origin) == ("yes", gate.ORIGIN_MODEL)

    model_mock.return_value = _ok("no")
    r2 = await gate.judge_page_fault("好，那我们继续刚才说的")           # 非纯寒暄整句
    assert (r2.decision, r2.origin) == ("no", gate.ORIGIN_MODEL)
    assert model_mock.await_count == 2
