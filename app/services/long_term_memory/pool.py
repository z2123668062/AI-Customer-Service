# app/services/long_term_memory/pool.py
"""快表：进程内存卡片池——有界工作集缓存（系统设计.md §3；决策记录 D18）。

定位：读侧第一站 + 写侧第二棒（D5 写序列：DB → 快表 → Qdrant）。数据库是唯一真相，
快表只是"下一帧读得动"的内存副本——**有容量上限（POOL_CAPACITY）**，溢出/未命中 = 缺页。

五结构（在册卡"同进同出"：四个索引 + 一个驻留策略）：
- 卡片字典  _cards         card_id → PoolCard 轻量快照（不持有 ORM 对象）
- 实体小抄  _entity_index  实体（归一化）→ card_id 倒排
- 线表镜像  _adjacency     card_id → {(邻居 id, kind)} 双向
- 时间轴    _timeline      (event_time, card_id) 有序
- 驻留策略  试用层 / 保护层（SLRU 两段）+ 换出记录簿（ghost）

替换策略（D18，两段式 + 记录簿）：
- 进池：写入/回填 → 试用层；换出记录簿命中（被换出过、又被要回）→ 直达保护层；
- 升层：试用层内被读到第二次 → 升保护层（配额 C×3/4，超额时最旧者降回试用层）；
- 换出：总量超上限时取试用层最旧者（记入记录簿）——替换（腾位，性能）与
  语义淘汰（下架，E11 正确性）严格分开；删除/取代不记记录簿。
- 确定性：顺序只由操作序列决定（无墙钟、无随机），测试可完全复现。

缺页机制（报告，而非搬运）：
- 读接口返回 (命中, 未命中清单)，并同步通知缺页钩子（register_fault_hook）——
  池永远零 IO、零 await；回源归 E9，回填经 refill()。
- 钩子契约：hook(kind, keys)，kind ∈ {"cards", "entities"}，同步调用、抛异常只告警。

写轨同步（D5 第二棒；E6 扩展；E11 再扩展）：store 钩子动作四件套——
"upsert"（卡快照进池）/ "delete"（出池，不进记录簿）/ "edge_upsert"（边进镜像；
补齐"边事件不在同步轨"缺口——顺线一跳不再漏新因果线）/ "edge_cut"（剪断边摘镜像，
与 edge_upsert 互逆——E11 编辑层）。

纯度：读接口与更新函数零 IO、零 await、无锁（进程内单副本）；唯一 IO 入口 = build()
（启动预热：最近创建的 C 张 active 卡 + 在册卡之间的线）。
"""
from __future__ import annotations

import bisect
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from app.core.logging import logger

# —— 取值约定（与 store.py 一致；勿散写字符串）——
_ACTIVE = "active"

# ============================================================
# 容量与替换参数（模块常量；E12 千卡压测校准——不做配置开关，D18）
# ============================================================

POOL_CAPACITY = 512   # 在册卡硬顶（工作集上限；"记忆本体"仍无限增长——上限只属于缓存）
PROTECTED_SHARE = 3   # 保护层配额 = C×3/4；超额 → 最旧者降回试用层（降级通道）
GHOST_SIZE = 512      # 换出记录簿长度（只存 id）：记住"最近被换出者"，承接复用证据


@dataclass(frozen=True)
class PoolCard:
    """卡片轻量快照：只读属性、零 ORM 依赖（D18——不直接持有 ORM 对象）。"""

    id: int
    kind: str
    subtype: str | None
    content: str
    entities: tuple[str, ...]
    event_time: datetime | None
    source: str
    created_at: datetime | None


# —— 五结构（4 索引 + 1 驻留策略）——
_cards: dict[int, PoolCard] = {}
_entity_index: dict[str, set[int]] = {}
_adjacency: dict[int, set[tuple[int, str]]] = {}
_timeline: list[tuple[datetime, int]] = []

_probation: OrderedDict[int, None] = OrderedDict()   # 试用层（MRU 在右端）
_protected: OrderedDict[int, None] = OrderedDict()   # 保护层
_ghost: OrderedDict[int, None] = OrderedDict()       # 换出记录簿（FIFO、有界）
_reads: dict[int, int] = {}                          # 试用层内"被读次数"（升层信号；出池即清）

# 缺页通知钩子（E9/埋点侧登记；池只报告、不搬运——零 IO 职责边界）
_fault_hook: Callable[[str, list], None] | None = None
_fault_count: dict[str, int] = {"cards": 0, "entities": 0}


def _protected_cap() -> int:
    return POOL_CAPACITY * PROTECTED_SHARE // 4


def _norm(text: str) -> str:
    """实体名归一：去首尾空白 + casefold（中文无影响，英文统一小写）。"""
    return text.strip().casefold()


def _as_utc(dt: datetime | None) -> datetime | None:
    """时区归一：naive 视为 UTC（SQLite 读回值），aware 一律转 UTC。

    池内时间轴必须单一口径——naive/aware 混存会让 bisect 比较直接 TypeError
    （来源有二：build 从 SQLite 读回 naive；写轨钩子是刚 flush 的 ORM 对象，
    extractor.fromisoformat 的结果可能带时区也可能不带）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _snapshot(card: Any) -> PoolCard:
    """ORM 卡 → 轻量快照（鸭子类型只读属性；须已 flush/commit，不触发任何 IO）。"""
    return PoolCard(
        id=card.id,
        kind=card.kind,
        subtype=card.subtype,
        content=card.content,
        entities=tuple(getattr(card, "entities", None) or ()),
        event_time=_as_utc(card.event_time),
        source=card.source,
        created_at=_as_utc(card.created_at),
    )


# ============================================================
# 索引维护（四结构——只在册卡；与卡片字典同进同出）
# ============================================================

def _index_add(card: PoolCard) -> None:
    for name in card.entities:
        key = _norm(name)
        if key:
            _entity_index.setdefault(key, set()).add(card.id)
    if card.event_time is not None:
        bisect.insort(_timeline, (card.event_time, card.id))


def _index_remove(card: PoolCard) -> None:
    for name in card.entities:
        key = _norm(name)
        ids = _entity_index.get(key)
        if ids is not None:
            ids.discard(card.id)
            if not ids:
                _entity_index.pop(key, None)
    if card.event_time is not None:
        pos = bisect.bisect_left(_timeline, (card.event_time, card.id))
        if pos < len(_timeline) and _timeline[pos] == (card.event_time, card.id):
            _timeline.pop(pos)


def _link(from_id: int, to_id: int, kind: str) -> None:
    """接线（严格镜像：两端都在册才装；换出/删除时同步摘除，不留在册单边）。"""
    if from_id == to_id or from_id not in _cards or to_id not in _cards:
        return
    _adjacency.setdefault(from_id, set()).add((to_id, kind))
    _adjacency.setdefault(to_id, set()).add((from_id, kind))


def _unlink(from_id: int, to_id: int, kind: str) -> None:
    """拆线（与 _link 互逆；E11 剪断轨）：双向邻接各摘一条——幂等（不在册边静默跳过）。

    与 _board_remove 的摘除区别：摘除 = 卡出池时的清扫（按卡全量摘）；拆线 = 边上
    的独立事件（卡可能仍在册）——只摘这一条线，不动卡与其他邻接。
    """
    for a, b in ((from_id, to_id), (to_id, from_id)):
        links = _adjacency.get(a)
        if links is None:
            continue
        links.discard((b, kind))
        if not links:
            _adjacency.pop(a, None)


def _board_remove(card_id: int) -> None:
    """从池中摘除（不带任何语义判断；是否记入记录簿由调用方决定）。"""
    card = _cards.pop(card_id, None)
    if card is None:
        return
    _index_remove(card)
    for neighbor_id, kind in _adjacency.pop(card_id, ()):
        links = _adjacency.get(neighbor_id)
        if links is not None:
            links.discard((card_id, kind))
            if not links:
                _adjacency.pop(neighbor_id, None)
    _probation.pop(card_id, None)
    _protected.pop(card_id, None)
    _reads.pop(card_id, None)


# ============================================================
# 驻留策略（SLRU 两段 + 换出记录簿）——D18
# ============================================================

def _ghost_add(card_id: int) -> None:
    """换出 → 记入记录簿（FIFO、有界；重复换出刷新位置）。"""
    if card_id in _ghost:
        del _ghost[card_id]
    _ghost[card_id] = None
    if len(_ghost) > GHOST_SIZE:
        _ghost.popitem(last=False)


def _demote_protected_lru() -> None:
    """保护层降级：最旧者回试用层（不是换出）——重攒升层信号，防两区振荡。"""
    victim, _ = _protected.popitem(last=False)
    _probation[victim] = None
    _reads[victim] = 0


def _enforce_protected_cap() -> None:
    while len(_protected) > _protected_cap():
        _demote_protected_lru()


def _enforce_limits() -> None:
    """总量硬顶：换出永远先找试用层最旧者；保护层直出仅兜底（正常到不了）。"""
    while len(_cards) > POOL_CAPACITY:
        if _probation:
            victim, _ = _probation.popitem(last=False)
            _board_remove(victim)
            _ghost_add(victim)
        elif _protected:  # pragma: no cover —— 配置病态时兜底
            victim, _ = _protected.popitem(last=False)
            _board_remove(victim)
            _ghost_add(victim)
        else:  # pragma: no cover
            break


def _place(card: PoolCard) -> None:
    """进池/更新：在册卡原地刷新；新卡——记录簿命中 → 直达保护层，否则进试用层。"""
    existing = _cards.get(card.id)
    if existing is not None:
        _index_remove(existing)
        _cards[card.id] = card
        _index_add(card)
        if card.id in _probation:
            _probation.move_to_end(card.id)
        elif card.id in _protected:
            _protected.move_to_end(card.id)
        return

    _cards[card.id] = card
    _index_add(card)
    if card.id in _ghost:            # 用成员判断：记录簿 value 就是 None，pop 的返回值分不清命中
        # 被换出过、又被要回 = 复用证据（D18）→ 直达保护层（治"升层饥饿"）
        del _ghost[card.id]
        _protected[card.id] = None
        _enforce_protected_cap()
    else:
        _probation[card.id] = None
        _reads[card.id] = 0
    _enforce_limits()


def _touch(card_id: int) -> None:
    """被读一次：保护层刷新 MRU；试用层攒升层信号（第二次被读 → 升保护层）。"""
    if card_id in _protected:
        _protected.move_to_end(card_id)
    elif card_id in _probation:
        _reads[card_id] = _reads.get(card_id, 0) + 1
        if _reads[card_id] >= 2:
            del _probation[card_id]
            _reads[card_id] = 0
            _protected[card_id] = None
            _enforce_protected_cap()
        else:
            _probation.move_to_end(card_id)


# ============================================================
# 读接口（三入口 + 内容取件）——命中即触碰；未命中返回清单 + 通知钩子
# ============================================================

def get_cards(card_ids: Sequence[int]) -> tuple[list[PoolCard], list[int]]:
    """按 id 取卡（内容读取入口）：返回 (命中卡, 未命中 id 清单)。

    命中即触碰（保护层刷新 / 试用层攒升层信号）；未命中 → 缺页通知（池不自己回源）。
    """
    hits: list[PoolCard] = []
    missing: list[int] = []
    for card_id in dict.fromkeys(card_ids):
        card = _cards.get(card_id)
        if card is None:
            missing.append(card_id)
        else:
            hits.append(card)
            _touch(card_id)
    if missing:
        _notify_fault("cards", missing)
    return hits, missing


def get_card(card_id: int) -> PoolCard | None:
    """单卡取件（未命中同样触发缺页通知）。"""
    hits, _ = get_cards([card_id])
    return hits[0] if hits else None


def by_entities(names: Sequence[str]) -> tuple[list[int], list[str]]:
    """实体入口：返回 (命中的 card_id 合并集, 未收录的实体名)。

    未收录的实体名 = 一级缺页线索（可能真没有，也可能被打出池）——通知钩子上报，
    由 E9 判断是否回源（池不做语义判断，零判断职责）。
    """
    hit: set[int] = set()
    missing: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        ids = _entity_index.get(key)
        if ids:
            hit.update(ids)
        else:
            missing.append(name)
    if missing:
        _notify_fault("entities", missing)
    return sorted(hit), missing


def known_entities() -> list[str]:
    """全部实体键（归一化名，按字典序）——E9 抽线索的"反向匹配"词表（只读零成本）。

    用途：用户原话没有结构化线索时，用已知实体名逐一对原话做子串匹配——
    "先有词表、再在话里找词"（E9 定稿；与 by_entities 的"话里有词、去表里查"互为反向）。
    返回新列表（副本），调用方可安全持有/改造。
    """
    return sorted(_entity_index.keys())


def by_time_range(start: datetime, end: datetime) -> list[int]:
    """时间入口：[start, end) 内的卡 id（按 event_time 升序；event_time 为空不入轴）。

    入参允许 naive 或 aware（统一归一 UTC 再比）——与池内时间轴的 _as_utc 口径对齐。
    """
    lo_key = _as_utc(start)
    hi_key = _as_utc(end)
    lo = bisect.bisect_left(_timeline, (lo_key, -1))
    hi = bisect.bisect_left(_timeline, (hi_key, -1))
    return [card_id for _, card_id in _timeline[lo:hi]]


def neighbors(card_id: int, limit: int | None = None) -> list[tuple[int, str]]:
    """顺线一跳：[(邻居 id, kind)]（按 (id, kind) 升序；limit 为顺线预取上限）。

    镜像只含"两端都在册"的线——被换出卡带走的线，由回填侧经 edge_upsert 补回（E9 定稿）。
    """
    links = _adjacency.get(card_id)
    if links is None:
        return []
    result = sorted(links)
    return result[:limit] if limit is not None else result


# ============================================================
# 写轨同步（D5 第二棒：store 钩子四动作）——同步纯内存，无 await
# ============================================================

def on_store_write(action: str, payload: Any = None) -> None:
    """store 写序列钩子（经 store.register_pool_hook 注册）。

    - "upsert"：payload = list[Card]（active 卡；非 active 防御性跳过）→ 进池/刷新；
    - "delete"：payload = list[int]（取代/撤销/删除/下架）→ 出池（语义移除 ≠ 换出：不入记录簿）；
    - "edge_upsert"：payload = list[(from_id, to_id, kind)] → 两端在册才装镜像；
    - "edge_cut"：payload = list[(from_id, to_id, kind)] → 摘邻接镜像（E11，幂等）。
    本函数永不抛出（失败只告警，不打断主链路——DB 已提交）。
    """
    try:
        if action == "upsert":
            for card in payload or ():
                if getattr(card, "status", _ACTIVE) != _ACTIVE:
                    continue
                _place(_snapshot(card))
        elif action == "delete":
            for card_id in payload or ():
                _board_remove(card_id)
        elif action == "edge_upsert":
            for from_id, to_id, kind in payload or ():
                _link(from_id, to_id, kind)
        elif action == "edge_cut":
            for from_id, to_id, kind in payload or ():
                _unlink(from_id, to_id, kind)
        else:  # pragma: no cover —— 契约外动作
            logger.warning(f"快表收到未知钩子动作：{action!r}")
    except Exception as e:  # noqa: BLE001 —— 钩子永不打断主链路
        logger.warning(f"快表钩子处理失败（不影响 DB 与回复）: {e}")


def refill(cards: Iterable[Any] = (), edges: Iterable[tuple[int, int, str]] = ()) -> None:
    """回源结果回填（E9 调用；池不负责回源——它只报告与收纳）。

    cards：回源取到的卡（ORM 卡或 PoolCard）；edges：顺带恢复的线（可选，两端在册才装）。
    """
    for card in cards:
        _place(card if isinstance(card, PoolCard) else _snapshot(card))
    for from_id, to_id, kind in edges:
        _link(from_id, to_id, kind)


# ============================================================
# 缺页通知（池只报告；回源 = E9，回填 = refill）
# ============================================================

def register_fault_hook(hook: Callable[[str, list], None] | None) -> None:
    """注册缺页通知钩子（E9/埋点侧登记；传 None 解除）。

    契约：hook(kind, keys)，**同步调用（无 await）**——
    - kind="cards"：keys = list[int]（读接口未命中的卡 id）；
    - kind="entities"：keys = list[str]（实体小抄未收录的实体名）。
    池只发通知、不做回源（零 IO 职责边界）；钩子抛异常只告警，不影响读接口返回。
    """
    global _fault_hook
    _fault_hook = hook


def _notify_fault(kind: str, keys: list) -> None:
    _fault_count[kind] = _fault_count.get(kind, 0) + 1
    if _fault_hook is None:
        return
    try:
        _fault_hook(kind, keys)
    except Exception as e:  # noqa: BLE001 —— 通知失败不影响返回
        logger.warning(f"缺页通知失败（不影响返回）: {e}")


# ============================================================
# 启动构建（唯一 IO 入口）与观测
# ============================================================

async def build(session: Any) -> dict[str, int]:
    """启动预热：最近创建的 C 张 active 卡 + 在册卡之间的线（唯一 IO 入口）。

    顺序纪律（main.py lifespan）：init_db → store.startup_check（先修复）→ pool.build →
    register_pool_hook。对账口径：构建结果 = 五结构与 DB 的可核对快照（stats 返回）。
    """
    from app.services.long_term_memory import store  # 懒导入：IO 只发生在构建边界

    clear()
    cards = await _list_all_active(session)
    if len(cards) > POOL_CAPACITY:
        cards = cards[-POOL_CAPACITY:]                 # 只留最近 C 张（id 升序尾部）
    for card in cards:
        _place(_snapshot(card))
    for edge in await store.list_edges(session):
        _link(edge.from_card_id, edge.to_card_id, edge.kind)
    report = stats()
    logger.info(f"快表构建完成：{report}")
    return report


async def _list_all_active(session: Any) -> list[Any]:
    """游标分页拉全量 active 卡（id 升序；不随并发写入漂移）。"""
    from app.services.long_term_memory import store

    result: list[Any] = []
    after_id: int | None = None
    while True:
        page = await store.list_cards(session, after_id=after_id, limit=500)
        result.extend(page)
        if len(page) < 500:
            return result
        after_id = page[-1].id


def stats() -> dict[str, int]:
    """观测快照（埋点/压测/诊断；只读，不变更任何次序）。"""
    return {
        "capacity": POOL_CAPACITY,
        "cards": len(_cards),
        "probation": len(_probation),
        "protected": len(_protected),
        "ghost": len(_ghost),
        "entities": len(_entity_index),
        "edges": sum(len(links) for links in _adjacency.values()) // 2,
        "timeline": len(_timeline),
        "faults_cards": _fault_count.get("cards", 0),
        "faults_entities": _fault_count.get("entities", 0),
    }


def tier_of(card_id: int) -> str | None:
    """在册卡驻留层："protected" / "probation"；不在册 None（诊断与测试用）。"""
    if card_id in _protected:
        return "protected"
    if card_id in _probation:
        return "probation"
    return None


def clear() -> None:
    """清空全部结构（启动重建与测试用）。"""
    _cards.clear()
    _entity_index.clear()
    _adjacency.clear()
    _timeline.clear()
    _probation.clear()
    _protected.clear()
    _ghost.clear()
    _reads.clear()
    _fault_count["cards"] = 0
    _fault_count["entities"] = 0
