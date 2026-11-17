# app/services/long_term_memory/reranker.py
"""读路径 · 精筛与门控：判定器逐张判档 + 折算硬阈值门 + 排序截断。

复用判定器（gate.judge_shortlist，单一出口）而非专用重排模型：
- 判定器给的是**价值档位**（low/mid/high）+ 理由 + 置信度——本环节要的核心判断是
  "这张卡该不该进模型的视野"（治理语义），不是相关性分数："相关但已过期"的卡，
  交叉编码器分不出，判定器能（prompt 内明确"包括已过期的旧状态 → low"）；
- 档位经固定映射折算分值（`_TIER_SCORES`）；硬阈值门（GATE_THRESHOLD）的语义 =
  **"到哪个档位才配注入"**——阈值是档位选择器，不是连续分数线；
- 不过门槛 = 整批丢弃（kept 为空）——"不召回"是默认路径的一部分，不是异常分支
  （"宁可不记，不可记错"铁律的检索侧体现，见系统设计 §4）。

降级纪律：
- gate 契约保证不抛（fail-safe=low → 不注入）；本模块仍对**单卡异常**兜底
  （单卡 low，不拖垮整批——批量判定的失败面隔离）；
- 排序确定性：折算分降序 → 置信度降序 → 稳定保序（输入序 = 粗排序序），
  无魔法权重。

接口：`rerank(query, candidates) -> RerankResult`（kept 可能为空）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Sequence

from app.core.config import settings
from app.core.logging import logger
from app.services.long_term_memory import gate
from app.services.long_term_memory.retriever import _ROUTE_RANK, Candidate

# ============================================================
# 精筛参数（模块常量；门槛与注入上限在 config：GATE_THRESHOLD / INJECT_MAX_CARDS）
# ============================================================

# 档位折算映射（宽间距——阈值放中间任何位置都明确落在"某两档之间"，见精筛注入文档 §3.1）：
#   high=0.9 / mid=0.6 / low=0.2；GATE_THRESHOLD=0.35 → mid+high 放行；>0.6 → 仅 high。
_TIER_SCORES = {"high": 0.9, "mid": 0.6, "low": 0.2}
_EXPLICIT_BONUS = 0.05     # 显式卡特征加分（读时优待）：同档内排序优先；小于档距 0.3，不越档
_JUDGE_CONCURRENCY = 16    # 精筛并发上限（信号量；RTT 主导延迟。L5：6→16，
                           # 延迟 ≈ 候选数÷并发×RTT，提高并发不改判定结果，只压墙钟）
_CARD_TEXT_LIMIT = 300     # 精筛输入卡文本截断（L2 原文候选可能很长，控制 prompt 成本）

# 关掉精筛时的兜底分（按入口给）：语义路有真实相似度，其余三路没有分，
# 不给兜底就会全部沉底、等于把实体与时间召回一起关掉。取值只求稳定可比，不追求最优。
_ROUTE_FALLBACK = {"entity": 0.60, "time": 0.55, "semantic": 0.50, "line": 0.50, "raw": 0.45}


def _fallback_score(candidate: Candidate) -> float:
    """无精筛时的排序分：有语义分用语义分，否则按最强入口给固定分。"""
    if candidate.semantic_score is not None:
        return float(candidate.semantic_score)
    best = min(candidate.routes, key=lambda r: _ROUTE_RANK.get(r, 9), default=None)
    return _ROUTE_FALLBACK.get(best, 0.45)


# ============================================================
# 数据契约（E10 核心产出 / trace 直接喂埋点 chain 组）
# ============================================================

@dataclass(frozen=True)
class ScoredCandidate:
    """一张候选的精筛结果（全字段进 trace items，供实验归因"为什么没注入"）。

    - tier：判定档位（low/mid/high；旁路档为空串——未判定）；
    - score：折算分 + explicit 加分后的**最终分**（门槛与排序都看它）；
    - reason / origin / latency_ms：判定器原样透传（兜底路径可读）。
    """
    candidate: Candidate
    tier: str
    score: float
    confidence: float
    reason: str
    origin: str
    latency_ms: float


@dataclass(frozen=True)
class RerankResult:
    """精筛的完整产出：过门槛候选（可能为空）+ trace（E12 并入埋点 chain 组）。"""
    kept: list[ScoredCandidate]
    trace: dict[str, Any]


# ============================================================
# trace 骨架（全字段恒定出现；对齐《配置与埋点文档》§2.3 rerank 组）
# ============================================================

def _empty_trace() -> dict[str, Any]:
    return {
        "threshold": settings.GATE_THRESHOLD,
        "max_cards": settings.INJECT_MAX_CARDS,
        "judged": 0,               # 实际判定张数
        "tiers": {"low": 0, "mid": 0, "high": 0},
        "passed": 0,               # 过门槛数（截断前）
        "injected": 0,             # 实际注入数（截断后）
        "truncated": 0,            # 超预算截断数（passed - injected）
        "items": [],               # 每张判定详情（按最终分降序）
        "stage_ms": {"rerank": 0.0},
    }


def _to_item(s: ScoredCandidate) -> dict[str, Any]:
    return {
        "ref_id": s.candidate.ref_id,
        "kind": s.candidate.kind,
        "tier": s.tier,
        "score": s.score,
        "confidence": s.confidence,
        "origin": s.origin,
        "latency_ms": s.latency_ms,
        "reason": s.reason,
    }


# ============================================================
# 单卡判定（并发基本单元）
# ============================================================

async def _judge_one(sem: asyncio.Semaphore, query: str,
                     candidate: Candidate) -> ScoredCandidate:
    """精筛一张：判定 → 折算分（+explicit 加分）。

    异常防御：gate 契约保证不抛；此处兜住"契约之外的意外"——单卡兜底 low
    （不注入方向），不拖垮整批（fail-safe 方向表）。
    """
    async with sem:
        try:
            result = await gate.judge_shortlist(query, candidate.content[:_CARD_TEXT_LIMIT])
        except Exception as exc:  # noqa: BLE001 —— 单卡防御兜底（不抛）
            logger.warning(f"[reranker] 单卡精筛异常兜底 ref_id={candidate.ref_id}: {exc}")
            result = gate.GateResult("low", 0.0, f"精筛异常兜底（{type(exc).__name__}）",
                                     gate.ORIGIN_FAILSAFE)
    tier = result.decision if result.decision in _TIER_SCORES else "low"
    score = _TIER_SCORES[tier]
    if candidate.source == "explicit":   # 读时优待：特征加分（同档内抢先、不越档）
        score = min(score + _EXPLICIT_BONUS, 1.0)
    return ScoredCandidate(candidate, tier, round(score, 4), result.confidence,
                           result.reason, result.origin, result.latency_ms)


# ============================================================
# 主入口
# ============================================================

async def rerank(query: str, candidates: Sequence[Candidate], *,
                 use_rerank: bool | None = None) -> RerankResult:
    """精筛主入口（粗排序候选 → 过门槛注入候选）。

    链路：并发逐张判定（信号量有界）→ 排序（分↓ / 置信度↓ / 输入序稳定）
    → 硬阈值门 → 截断 INJECT_MAX_CARDS。

    use_rerank 为逐轮覆盖（对话页开关）：None = 跟随服务端配置。
    """
    enabled = settings.RERANK_ENABLED if use_rerank is None else use_rerank
    t0 = time.perf_counter()
    trace = _empty_trace()

    if not candidates:
        trace["stage_ms"]["rerank"] = round((time.perf_counter() - t0) * 1000, 1)
        return RerankResult([], trace)

    # ① 判定：默认逐张精筛；关闭时走旁路（零 LLM 调用）
    if enabled:
        sem = asyncio.Semaphore(_JUDGE_CONCURRENCY)
        scored = list(await asyncio.gather(*(_judge_one(sem, query, c) for c in candidates)))
    else:
        scored = [
            ScoredCandidate(c, "", round(_fallback_score(c), 4), 0.0,
                            "未判定（精筛关闭，按语义分与入口分排序）", "bypass", 0.0)
            for c in candidates
        ]
        trace["bypass"] = True

    # ② 排序：最终分降序 → 置信度降序 → 稳定保序（输入粗排序）
    scored.sort(key=lambda s: (-s.score, -s.confidence))

    # ③ 硬阈值门（按含 explicit 加分后的最终分）；④ 截断
    passed = [s for s in scored if s.score >= settings.GATE_THRESHOLD]
    kept = passed[:settings.INJECT_MAX_CARDS]

    # judged = 实际发生的 LLM 判定次数（埋点 cost.llm_calls 直接读它）。
    # 关精排时一张都没判，记 0；候选总量另由 items/truncated 体现。
    trace["judged"] = len(scored) if enabled else 0
    for s in scored:
        if s.tier in trace["tiers"]:
            trace["tiers"][s.tier] += 1
    trace["passed"] = len(passed)
    trace["injected"] = len(kept)
    trace["truncated"] = len(passed) - len(kept)
    trace["items"] = [_to_item(s) for s in scored]
    trace["stage_ms"]["rerank"] = round((time.perf_counter() - t0) * 1000, 1)
    logger.debug(f"[reranker] 精筛完成：判 {len(scored)} / 过门 {len(passed)} / 注入 {len(kept)}"
                 f"（tiers={trace['tiers']}，{trace['stage_ms']['rerank']}ms）")
    return RerankResult(kept, trace)
