# app/services/long_term_memory/writer.py
"""写路径 · 落库三固定动作（E8）：归位（取代判定）+ 回填因果 + 同步。

两段式（防长事务锁库；LLM 判定绝不在事务里跑）：
    load_candidates()（事务内读、导快照）→ plan_batch()（事务外调 gate 判定）
    → apply_batch()（短事务：建卡/取代/连边，只 flush）→ 调用方 commit_and_sync 收口。

三固定动作（系统设计.md §5）：
① 归位（有候选才判；无候选不调判定器直接新增）：gate.judge_supersede(旧卡, 新信息) 三选一——
   unrelated → 新增；coexist → 新增（不连线）；supersede → 新卡 + supersede_card（同事务两语句）。
② 回填（等答案机制）：新 event 草稿 × “active、更早、无 caused_by 出边”的旧 event 卡
   （取最近 _BACKFILL_TOP 张）——gate.judge_backfill(旧卡, 新卡) 判因果；yes → 建 caused_by 边
   from=旧（结果）to=新（原因）。V1 只处理“先说结果后补原因”顺序（固定方向约定；
   误判由 gate 保守性兜底——fail-safe=no）。
③ 同步：不在这里——apply 只返回 IngestResult（含待同步卡/边），由 consolidator 调
   store.commit_and_sync 收口（唯一提交点）。

判重：草稿与候选池同 kind 卡**逐字同文** → 拦截不建卡（重抽幂等化的简单兜底；
语义重复仍走归位/取代收敛）。比对范围 = 最近 _CANDIDATE_SCAN 张同 kind active 卡
（与候选筛选同一读窗）。

降级纪律：草稿未过 store 校验 → 丢弃计数（不拖垮整批）；旧卡已被并发批次取代 → 跳过该动作。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.models.domain import Card, Edge
from app.services.long_term_memory import gate, store
from app.services.long_term_memory.extractor import CardDraft

_SUPERSEDE_TOP = 2      # 归位候选数（每草稿最多比对 2 张旧卡）
_BACKFILL_TOP = 2       # 回填候选数（“等答案”旧卡最多 2 张）
_CANDIDATE_SCAN = 200   # 候选池扫描上限（最近 N 张同 kind active 卡）
_MAX_PLAN_CALLS = 40    # 单批判定调用上限（防极端批打爆 LLM；超限即停止判定、余下按全新增）


@dataclass(frozen=True, slots=True)
class CardSnapshot:
    """候选卡快照（事务内导出、事务外判定用——脱离 session 的安全副本）。"""

    id: int
    kind: str
    subtype: str | None
    content: str
    entities: tuple[str, ...]


@dataclass(slots=True)
class Candidates:
    """候选集：归位候选（逐草稿索引）+ 回填候选（全批共用）+ 完全相同卡（判重）。"""

    supersede: dict[int, list[CardSnapshot]] = field(default_factory=dict)
    backfill: list[CardSnapshot] = field(default_factory=list)
    exact_duplicates: dict[int, int] = field(default_factory=dict)   # 草稿索引 → 同文既有卡 id


@dataclass(slots=True)
class BatchPlan:
    """判定后的落库计划（apply 的输入；drafts 顺序即建卡顺序，索引对齐）。"""

    drafts: list[CardDraft] = field(default_factory=list)
    supersedes: dict[int, int] = field(default_factory=dict)         # 草稿索引 → 被取代旧卡 id
    backfills: list[tuple[int, int]] = field(default_factory=list)   # (旧卡 id, 草稿索引)
    exact_duplicates: dict[int, int] = field(default_factory=dict)   # 判重拦截（不建卡）


@dataclass(slots=True)
class IngestResult:
    """一次落库的产出（含待同步清单——commit_and_sync 的入参）。"""

    cards_created: int = 0
    edges_created: int = 0
    superseded: int = 0
    drafts_dropped: int = 0
    duplicates: int = 0                      # 完全相同卡判重拦截数（E12 接活；journal `重复=`）
    cards: list[Card] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


# ============ ① 候选筛选（事务内读；确定性规则）============

def _card_snapshot(card: Card) -> CardSnapshot:
    return CardSnapshot(id=card.id, kind=card.kind, subtype=card.subtype,
                        content=card.content, entities=tuple(card.entities or ()))


def _is_related(draft: CardDraft, card: CardSnapshot) -> bool:
    """确定性候选规则：实体交集 或（entity 卡）subtype 相同。"""
    if draft.entities and card.entities and set(draft.entities) & set(card.entities):
        return True
    if draft.kind == "entity" and draft.subtype and draft.subtype == card.subtype:
        return True
    return False


async def load_candidates(session: AsyncSession, *, drafts: Sequence[CardDraft]) -> Candidates:
    """候选卡筛选（事务内调用；只读）。

    - 判重：草稿与同 kind 候选池逐字同文 → 记入 exact_duplicates（不填归位候选、不建卡）；
    - 归位候选：同 kind、active、按 id 降序最近 _CANDIDATE_SCAN 张 → 实体交集或 subtype 相同
      → 每草稿取最近 _SUPERSEDE_TOP 张；
    - 回填候选：active event 卡、无 caused_by 出边（“还没被解释的结果”）→ 取最近 _BACKFILL_TOP 张。
    """
    result = Candidates()
    kinds = {d.kind for d in drafts}
    pools: dict[str, list[CardSnapshot]] = {}
    for kind in kinds:
        cards = await store.list_cards(session, kind=kind, status=store.ACTIVE,
                                       desc=True, limit=_CANDIDATE_SCAN)
        pools[kind] = [_card_snapshot(c) for c in cards]
    for idx, draft in enumerate(drafts):
        pool = pools.get(draft.kind, [])
        for card in pool:                                    # 判重：与既有卡逐字同文
            if draft.content == card.content:
                result.exact_duplicates[idx] = card.id
                break
        if idx in result.exact_duplicates:                   # 同文草稿不建卡 → 无需归位候选
            continue
        cands = [c for c in pool if _is_related(draft, c)]
        if cands:
            result.supersede[idx] = cands[:_SUPERSEDE_TOP]
    if "event" in kinds:
        explained = {e.from_card_id for e in await store.list_edges(session, kind="caused_by")}
        pool = pools.get("event", [])
        result.backfill = [c for c in pool if c.id not in explained][:_BACKFILL_TOP]
    return result


# ============ ② 判定（事务外；调 gate）============

async def plan_batch(*, drafts: Sequence[CardDraft], candidates: Candidates) -> BatchPlan:
    """事务外判定：调 gate 产出落库计划（gate 已全兜底，本函数绝不抛）。

    判重拦截的草稿原样带入计划、不参与归位/回填判定（省无谓 LLM 调用）。
    """
    plan = BatchPlan(drafts=list(drafts), exact_duplicates=dict(candidates.exact_duplicates))
    dup = set(candidates.exact_duplicates)
    calls = 0
    # 归位（取代）判定
    claimed: set[int] = set()
    for idx, draft in enumerate(drafts):
        if idx in dup:
            continue
        best: tuple[float, int] | None = None
        for cand in candidates.supersede.get(idx, []):
            if cand.id in claimed:
                continue
            if calls >= _MAX_PLAN_CALLS:
                logger.warning(f"[writer] 判定调用达上限 {_MAX_PLAN_CALLS}，余下草稿按全新增处理")
                return plan
            verdict = await gate.judge_supersede(cand.content, draft.content)
            calls += 1
            if verdict.decision == "supersede" and (best is None or verdict.confidence > best[0]):
                best = (verdict.confidence, cand.id)
        if best is not None:
            plan.supersedes[idx] = best[1]
            claimed.add(best[1])
    # 回填判定（等答案机制；仅 event 草稿）
    if candidates.backfill:
        claimed_bf: set[int] = set()
        for idx, draft in enumerate(drafts):
            if draft.kind != "event" or idx in dup:
                continue
            best = None
            for cand in candidates.backfill:
                if cand.id in claimed_bf:
                    continue
                if calls >= _MAX_PLAN_CALLS:
                    logger.warning(f"[writer] 判定调用达上限 {_MAX_PLAN_CALLS}，停止回填判定")
                    return plan
                verdict = await gate.judge_backfill(cand.content, draft.content)
                calls += 1
                if verdict.decision == "yes" and (best is None or verdict.confidence > best[0]):
                    best = (verdict.confidence, cand.id)
            if best is not None:
                plan.backfills.append((best[1], idx))
                claimed_bf.add(best[1])
    return plan


# ============ ③ 落库（短事务；只 flush，提交由调用方收口）============

async def apply_batch(
    session: AsyncSession,
    *,
    plan: BatchPlan,
    session_id: str,
    source: str = "passive",
) -> IngestResult:
    """落库计划执行：判重拦截 → 建卡 → 取代（同事务两语句）→ 回填连边（只 flush 不 commit）。"""
    result = IngestResult()
    created: dict[int, int] = {}
    for idx, draft in enumerate(plan.drafts):
        if idx in plan.exact_duplicates:                     # 判重：与既有卡同文 → 拦截不建
            result.duplicates += 1
            logger.info(f"[writer] 草稿与既有卡 {plan.exact_duplicates[idx]} 完全同文，拦截不建卡")
            continue
        try:
            card = await store.create_card(session, kind=draft.kind, content=draft.content,
                                           subtype=draft.subtype,
                                           entities=list(draft.entities) or None,
                                           event_time=draft.event_time, source=source,
                                           source_session_id=session_id)
        except (ValueError, NotImplementedError) as e:      # 防御：草稿校验兜底
            result.drafts_dropped += 1
            logger.warning(f"[writer] 草稿落库被拒（丢弃）: {e}")
            continue
        created[idx] = card.id
        result.cards.append(card)
        result.cards_created += 1
    # 取代（版本化：新卡顶旧卡 + supersedes 边——同事务）
    for idx, old_id in plan.supersedes.items():
        new_id = created.get(idx)
        if new_id is None:
            continue
        old = await store.get_card(session, old_id)
        if old is None or old.status != store.ACTIVE:       # 并发保护：已被别的批次顶掉
            logger.info(f"[writer] 旧卡 {old_id} 已非 active（并发更新），跳过取代")
            continue
        edge = await store.supersede_card(session, new_card_id=new_id, old_card_id=old_id,
                                          source_session_id=session_id)
        result.edges.append(edge)
        result.edges_created += 1
        result.superseded += 1
    # 回填（caused_by 边 from=旧（结果）to=新（原因））
    for old_id, idx in plan.backfills:
        new_id = created.get(idx)
        if new_id is None:
            continue
        try:
            edge = await store.create_edge(session, from_card_id=old_id, to_card_id=new_id,
                                           kind="caused_by", source_session_id=session_id)
        except ValueError as e:
            logger.warning(f"[writer] 回填连线被拒: {e}")
            continue
        result.edges.append(edge)
        result.edges_created += 1
    return result


async def apply_plain(
    session: AsyncSession,
    *,
    drafts: Sequence[CardDraft],
    session_id: str,
    source: str = "passive",
) -> IngestResult:
    """直建卡（无判定）——B4′ 原文块与其它“零归位”形态入口。"""
    return await apply_batch(session, plan=BatchPlan(drafts=list(drafts)),
                             session_id=session_id, source=source)
