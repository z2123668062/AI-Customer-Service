"""精筛单测（E10）：折算映射 / 硬阈值门 / 排序截断 / 旁路 / explicit 加分 / 兜底 / trace。

策略：恒 monkeypatch `gate.judge_shortlist`（精筛单测不真调 LLM）——reranker 的
编排与门控语义是被测对象；GATE_* 配置经 monkeypatch 覆盖（测试后自动还原）。
"""
import asyncio

import pytest

from app.core.config import settings
from app.services.long_term_memory import gate, reranker
from app.services.long_term_memory.retriever import Candidate


def _cand(i, content=None, kind="entity", source="passive", line_kind=None):
    """真 Candidate（E9 契约），只填精筛关心的字段。"""
    return Candidate(
        ref_id=i, kind=kind, subtype=None, content=content or f"卡{i}",
        event_time=None, source=source, created_at=None,
        routes=frozenset({"entity"}), semantic_score=None, line_kind=line_kind,
    )


def _stub(monkeypatch, fn):
    """替身精筛：fn(card 文本) → tier；返回 'raise' 表示模拟异常。"""
    async def fake(query, card):
        tier = fn(card)
        if tier == "raise":
            raise RuntimeError("stub boom")
        return gate.GateResult(tier, 0.9, f"stub:{tier}", gate.ORIGIN_MODEL, 5.0)
    monkeypatch.setattr(gate, "judge_shortlist", fake)


def _by_content(mapping, default="low"):
    return lambda card: mapping.get(card, default)


@pytest.fixture(autouse=True)
def _reset_gate_config(monkeypatch):
    """每测试：阈值 0.35、上限 3（用例自行覆盖）。"""
    monkeypatch.setattr(settings, "GATE_THRESHOLD", 0.35)
    monkeypatch.setattr(settings, "INJECT_MAX_CARDS", 3)
    yield


# ================================
# 空输入 / 折算 / 门控
# ================================

@pytest.mark.asyncio
async def test_empty_candidates_returns_empty(monkeypatch):
    _stub(monkeypatch, lambda card: "high")
    result = await reranker.rerank("q", [])
    assert result.kept == [] and result.trace["judged"] == 0


@pytest.mark.asyncio
async def test_tier_scoring_and_gate(monkeypatch):
    """三档折算（0.9/0.6/0.2）+ 默认门（0.35）：mid+high 过、low 丢、按分降序。"""
    _stub(monkeypatch, _by_content({"卡1": "mid", "卡2": "high", "卡3": "low"}))
    result = await reranker.rerank("q", [_cand(1), _cand(2), _cand(3)])
    assert [s.candidate.ref_id for s in result.kept] == [2, 1]     # high(0.9) → mid(0.6)
    assert [s.score for s in result.kept] == [0.9, 0.6]
    assert result.trace["tiers"] == {"low": 1, "mid": 1, "high": 1}
    assert (result.trace["passed"], result.trace["injected"]) == (2, 2)


@pytest.mark.asyncio
async def test_all_low_drops_all_kept_empty(monkeypatch):
    """陷阱题验收：全部 low（不相关/已过期）→ 一条不注入（"不召回"档位）。"""
    _stub(monkeypatch, lambda card: "low")
    result = await reranker.rerank("q", [_cand(1), _cand(2)])
    assert result.kept == []
    assert result.trace["injected"] == 0 and result.trace["passed"] == 0


@pytest.mark.asyncio
async def test_threshold_knob_high_only(monkeypatch):
    """阈值旋钮：调到 0.7（>mid 0.6）→ 只放行 high。"""
    monkeypatch.setattr(settings, "GATE_THRESHOLD", 0.7)
    _stub(monkeypatch, _by_content({"卡1": "high", "卡2": "mid"}))
    result = await reranker.rerank("q", [_cand(1), _cand(2)])
    assert [s.candidate.ref_id for s in result.kept] == [1]


# ================================
# 排序 / 截断 / explicit 加分
# ================================

@pytest.mark.asyncio
async def test_truncate_to_inject_max_cards(monkeypatch):
    """超预算截断：5 张过门 → 只注入前 3（truncated=2）。"""
    _stub(monkeypatch, lambda card: "high")
    result = await reranker.rerank("q", [_cand(i) for i in range(1, 6)])
    assert [s.candidate.ref_id for s in result.kept] == [1, 2, 3]
    assert result.trace["passed"] == 5 and result.trace["truncated"] == 2


@pytest.mark.asyncio
async def test_confidence_tiebreak_within_same_tier(monkeypatch):
    """同档同分：置信度降序（其余保持输入序）。"""
    async def fake(query, card):
        conf = 0.9 if card == "卡1" else 0.7   # 卡1 置信度更高但在输入里靠后
        return gate.GateResult("high", conf, "stub", gate.ORIGIN_MODEL, 5.0)
    monkeypatch.setattr(gate, "judge_shortlist", fake)
    result = await reranker.rerank("q", [_cand(2, "卡2"), _cand(1, "卡1")])
    assert [s.candidate.ref_id for s in result.kept] == [1, 2]


@pytest.mark.asyncio
async def test_stable_order_preserved_for_identical_scores(monkeypatch):
    """全同分同置信度：稳定保序（= 输入序 = E9 粗排序序）。"""
    _stub(monkeypatch, lambda card: "high")
    result = await reranker.rerank("q", [_cand(3), _cand(1), _cand(2)])
    assert [s.candidate.ref_id for s in result.kept] == [3, 1, 2]


@pytest.mark.asyncio
async def test_explicit_bonus_orders_before_same_tier(monkeypatch):
    """显式优待①：同档内 explicit 加分（0.65 > 0.6）排前。"""
    _stub(monkeypatch, lambda card: "mid")
    result = await reranker.rerank("q", [_cand(1), _cand(2, source="explicit")])
    assert [s.candidate.ref_id for s in result.kept] == [2, 1]
    assert result.kept[0].score == 0.65


@pytest.mark.asyncio
async def test_explicit_bonus_does_not_cross_gate(monkeypatch):
    """显式优待②：不越档——explicit low（0.25）仍低于门槛 0.35，不注入。"""
    _stub(monkeypatch, lambda card: "low")
    result = await reranker.rerank("q", [_cand(1, source="explicit")])
    assert result.kept == []


# ================================
# 兜底 / 并发 / trace
# ================================

@pytest.mark.asyncio
async def test_single_card_failure_falls_back_low_not_break_batch(monkeypatch):
    """单卡异常兜底：坏卡按 low 丢，好卡照常注入（失败面隔离）。"""
    _stub(monkeypatch, _by_content({"卡2": "raise"}))   # 卡2 抛异常，其余 low
    result = await reranker.rerank("q", [_cand(1, "卡1"), _cand(2, "卡2")])
    assert result.kept == []
    bad = next(i for i in result.trace["items"] if i["ref_id"] == 2)
    assert bad["tier"] == "low" and bad["origin"] == gate.ORIGIN_FAILSAFE


@pytest.mark.asyncio
async def test_concurrency_bounded_by_semaphore(monkeypatch):
    """并发有界：同时在飞的判定调用 ≤ _JUDGE_CONCURRENCY。"""
    state = {"cur": 0, "peak": 0}

    async def fake(query, card):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.005)
        state["cur"] -= 1
        return gate.GateResult("high", 0.9, "x", gate.ORIGIN_MODEL, 1.0)
    monkeypatch.setattr(gate, "judge_shortlist", fake)

    await reranker.rerank("q", [_cand(i) for i in range(1, 13)])
    assert state["peak"] <= reranker._JUDGE_CONCURRENCY


@pytest.mark.asyncio
async def test_trace_fields_complete(monkeypatch):
    """trace 骨架全字段（E12 埋点按此并 chain 组）。"""
    _stub(monkeypatch, _by_content({"卡1": "high", "卡2": "low"}))
    result = await reranker.rerank("q", [_cand(1, "卡1"), _cand(2, "卡2")])
    trace = result.trace
    assert set(trace) == {"threshold", "max_cards", "judged", "tiers",
                          "passed", "injected", "truncated", "items", "stage_ms"}
    assert trace["judged"] == 2
    item = trace["items"][0]
    assert set(item) == {"ref_id", "kind", "tier", "score", "confidence",
                         "origin", "latency_ms", "reason"}
    assert trace["stage_ms"]["rerank"] >= 0.0


@pytest.mark.asyncio
async def test_long_raw_candidate_text_truncated_for_judge(monkeypatch):
    """精筛输入卡文本截断（_CARD_TEXT_LIMIT）——raw 原文候选可能很长。"""
    seen = {}

    async def fake(query, card):
        seen["len"] = len(card)
        return gate.GateResult("high", 0.9, "x", gate.ORIGIN_MODEL, 1.0)
    monkeypatch.setattr(gate, "judge_shortlist", fake)

    long_text = "长" * (reranker._CARD_TEXT_LIMIT + 500)
    await reranker.rerank("q", [_cand(1, long_text, kind="raw")])
    assert seen["len"] == reranker._CARD_TEXT_LIMIT
