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

import re
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


_TEXT_STRIP_RE = re.compile(r"[\W_]+")
_DUPE_MIN_CHARS = 15       # 被判重的最短正文长度（短句互相包含是常态，不能当重复）
_DUPE_RATIO = 0.7          # 长度比下限：短的至少占长的七成，才允许用「包含」判重


def _norm_text(text: str) -> str:
    """判重用的归一化：去空白与标点、统一大小写（与注入侧 _norm 同口径）。"""
    return _TEXT_STRIP_RE.sub("", text or "").casefold()


def _looks_duplicate(draft_content: str, card_content: str) -> bool:
    """判重口径（L6 放宽）：归一化后完全相同，或短的一方被长的一方完整包含。

    原实现只认逐字同文，「Melanie has children」与「Melanie has children.」都不算重复，
    209 张卡里出现了成对的家人口味卡，各占一个注入名额。放宽时仍取保守方向：
    短句不足 15 字或长度比低于 0.7 一律不判重（防「用户」吞掉整段）。
    """
    a, b = _norm_text(draft_content), _norm_text(card_content)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return (len(short) >= _DUPE_MIN_CHARS
            and len(short) / len(long) >= _DUPE_RATIO
            and short in long)


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
        # L7：节点卡（人/地/物）不进候选池。它不是"事实"，不该被取代判定或判重误伤——
        # 属性卡和节点卡的 entities 都含 "Caroline"，混在一起会拿一个人的节点去比一条偏好。
        pools[kind] = [_card_snapshot(c) for c in cards if not c.canonical_name]
    for idx, draft in enumerate(drafts):
        pool = pools.get(draft.kind, [])
        for card in pool:                                    # 判重：归一化同文或包含关系
            if _looks_duplicate(draft.content, card.content):
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
        if idx in dup or draft.canonical_name:      # 节点卡（人/地/物）不参与取代判定：它不是"事实"
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
    """落库计划执行：判重拦截 → 建卡 → 取代（同事务两语句）→ 回填连边（只 flush 不 commit）。

    L7 追加两件事：节点卡按标准名归并（不重复建人/地/物），事件卡建完后连 led_to 到节点。
    """
    result = IngestResult()
    created: dict[int, int] = {}
    node_index = await _load_node_index(session)      # 标准名/别名 → 卡 id
    for idx, draft in enumerate(plan.drafts):
        if idx in plan.exact_duplicates:                     # 判重：与既有卡同文 → 拦截不建
            result.duplicates += 1
            logger.info(f"[writer] 草稿与既有卡 {plan.exact_duplicates[idx]} 完全同文，拦截不建卡")
            continue
        if draft.canonical_name:                             # 节点卡：同一个人/地点只留一张
            keys = [_norm_text(x) for x in (draft.entities or (draft.canonical_name,))]
            hit = next((node_index[k] for k in keys if k in node_index), None)
            if hit is not None:
                created[idx] = hit
                result.duplicates += 1
                continue
        try:
            card = await store.create_card(session, kind=draft.kind, content=draft.content,
                                           subtype=draft.subtype,
                                           entities=list(draft.entities) or None,
                                           event_time=draft.event_time, source=source,
                                           source_session_id=session_id,
                                           canonical_name=draft.canonical_name,
                                           source_message_ids=list(draft.source_message_ids) or None)
        except (ValueError, NotImplementedError) as e:      # 防御：草稿校验兜底
            result.drafts_dropped += 1
            logger.warning(f"[writer] 草稿落库被拒（丢弃）: {e}")
            continue
        created[idx] = card.id
        result.cards.append(card)
        result.cards_created += 1
        if draft.canonical_name:                             # 新节点进索引（含别名）
            for name in (draft.entities or ()):
                node_index.setdefault(_norm_text(name), card.id)
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
    # L7：位线（led_to）——事件卡指向它涉及的人/地点节点。
    # 这是「谁、在哪里」第一次变成可以顺着走的路径；在此之前它们只是正文里的字符串。
    linked: set[tuple[int, int]] = set()
    for idx, card_id in created.items():
        draft = plan.drafts[idx]
        if draft.kind != "event":
            continue
        for name in draft.entities:
            node_id = node_index.get(_norm_text(name))
            if node_id is None or node_id == card_id or (card_id, node_id) in linked:
                continue
            linked.add((card_id, node_id))
            try:
                edge = await store.create_edge(session, from_card_id=card_id, to_card_id=node_id,
                                               kind="led_to", source_session_id=session_id)
            except ValueError as e:
                logger.warning(f"[writer] led_to 连线被拒: {e}")
                continue
            result.edges.append(edge)
            result.edges_created += 1
    return result


async def _load_node_index(session: AsyncSession) -> dict[str, int]:
    """库里已有的节点索引（标准名与别名 → 卡 id），供本批归并与连线。

    只认 canonical_name 非空的卡；属性卡（身份/偏好/目标/关系）不参与，它们不是端点。
    """
    cards = await store.list_cards(session, kind="entity", status=store.ACTIVE, limit=None)
    index: dict[str, int] = {}
    for card in cards:
        if not card.canonical_name:
            continue
        for name in [card.canonical_name, *(card.entities or [])]:
            index.setdefault(_norm_text(str(name)), card.id)
    return index


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
