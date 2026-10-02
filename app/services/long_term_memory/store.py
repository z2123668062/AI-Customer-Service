# app/services/long_term_memory/store.py
"""存储封装：三表 CRUD + 写序列编排 + 一致性铁律 + Qdrant 向量面（系统设计.md §2.4 / §3 / §9）。

冷层 = 记忆的完整持久形态：卡片与线在 cards/edges 表，语义向量在 Qdrant，原文在 messages 表。
上层模块只通过 store 访问冷层，不直接碰 ORM / 向量库（架构硬约束②：记忆包自治）。

E5 交付（2026-09-26；详细设计见 docs/存储封装文档.md）：
- 三表 CRUD：卡（create/get/list/revoke/bump_access）· 边（create/get/list）· 明文（append/list）——
  原语一律只 flush 不 commit，唯一提交点 = commit_and_sync()；
- 写序列编排（D5）：DB 事务提交 → 快表钩子（同步纯内存、无 await）→ Qdrant（失败落重试队列）；
- 一致性：check_and_rebuild_status()（探测漂移 → 告警 + rebuild_status）；startup_check() 启动钩子；
- 向量面（D17）：Qdrant 只装 active 卡——取代/撤销即删旧向量，启动对账保证
  "集合 = DB active 集合"；embedding 双 provider（api 默认 / local 兜底）。

E2 原语（supersede_card / rebuild_status）为一致性核心，保持不动。

E8 扩充（2026-09-28；D23 修订 2026-09-29）：`list_cards` 的 desc 参数（写路径“最近卡”候选筛选）+
`get_global_message_stats`（全局消息统计）。水位已迁 journal 日志（D23），会话状态原语已删除。
设计见 docs/写路径文档.md。

E11 扩充（2026-10-01；淘汰与编辑层，设计见 docs/淘汰与编辑层文档.md）：
- status 第四值 `evicted`（衰减下架，权威标记——rebuild 不动）；
- 新原语：`evict_cards`（批量下架）· `restore_card`（恢复）· `rollback_card`（版本回滚）·
  `cut_edge`（剪断因果边，标记式）· `purge_card`（被遗忘权：物理删卡 + 相关边）；
- `edges.cut_at` 剪断列：剪断的边不参与顺线/回填/版本重建（`list_edges` 默认排除）；
- `commit_and_sync` 新增 `cut_edges` 同步轨（快表邻接镜像摘边）。

依赖方向：被 retriever / writer / evictor / memory 接口层使用；不得反向依赖生成与 HTTP 层。
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.models.domain import (CARD_KINDS, CARD_SOURCES, CARD_SUBTYPES_ENTITY,
                               Card, EDGE_KINDS, Edge, Message)

# —— 取值约定（与 domain.CARD_STATUSES / EDGE_KINDS 一致；勿散写字符串）——
ACTIVE = "active"
SUPERSEDED = "superseded"
REVOKED = "revoked"
EVICTED = "evicted"          # 衰减下架（E11；权威标记，rebuild 不动）
KIND_SUPERSEDES = "supersedes"


async def supersede_card(
    session: AsyncSession,
    *,
    new_card_id: int,
    old_card_id: int,
    source_session_id: str | None = None,
) -> Edge:
    """取代 = 一个动作两条语句：① 插 supersedes 边（新→旧）；② 旧卡 status 改 superseded。

    铁律（工程规范 §2）：两条语句必须同 DB 事务——本函数与调用方共用 session，只
    add / flush、**不 commit**，由唯一写入口统一提交；中途失败则整体回滚（不许撕裂）。

    约定：新卡须已建好且为 active（本函数只动旧卡）；"已撤销（revoked）"是用户删除
    动作，不走本函数、不走版本链（软删 + 剪边标记）。
    """
    edge = Edge(
        from_card_id=new_card_id,
        to_card_id=old_card_id,
        kind=KIND_SUPERSEDES,
        source_session_id=source_session_id,
    )
    session.add(edge)
    await session.execute(
        update(Card)
        .where(Card.id == old_card_id)
        .values(status=SUPERSEDED)
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()
    return edge


async def rebuild_status(session: AsyncSession) -> int:
    """从边重算全部卡片的 status（物化视图修复），返回修正条数。

    规则（工程规范 §2 / 系统设计 §2.4；E11 修订）：
    - 有**未剪断**入边 supersedes 的卡 → superseded（"被更新的新卡指向"即被取代）；
    - 无未剪断入边 → active（回荡被误标/剪断/回滚的）；
    - revoked / evicted 是权威标记（用户撤销 / 衰减下架），不由边推导，**保持不动**。

    用途：E5 起在启动时调用做一致性检查——不一致 → 告警 + 本函数自动重建（兜底）。
    """
    incoming_supersedes = select(Edge.to_card_id).where(
        Edge.kind == KIND_SUPERSEDES, Edge.cut_at.is_(None))

    # ① 漏改的：已有未剪断入边、状态却没标 superseded（排除 revoked / evicted / 已是 superseded 的）
    fixed1 = await session.execute(
        update(Card)
        .where(Card.status.notin_([SUPERSEDED, REVOKED, EVICTED]), Card.id.in_(incoming_supersedes))
        .values(status=SUPERSEDED)
        .execution_options(synchronize_session="fetch")
    )
    # ② 回荡的：标着 superseded、但未剪断入边已不在（边被剪断/回滚等）
    fixed2 = await session.execute(
        update(Card)
        .where(Card.status == SUPERSEDED, Card.id.notin_(incoming_supersedes))
        .values(status=ACTIVE)
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()
    return (fixed1.rowcount or 0) + (fixed2.rowcount or 0)


# ============================================================
# 三表 CRUD（E5）：原语一律只 flush 不 commit，唯一提交点 = commit_and_sync()
# ============================================================

# —— 卡片 ——

async def create_card(
    session: AsyncSession,
    *,
    kind: str,
    content: str,
    subtype: str | None = None,
    entities: list[str] | None = None,
    event_time: datetime | None = None,
    source: str = "passive",
    source_session_id: str | None = None,
    importance: float | None = None,
) -> Card:
    """建卡（唯一建卡入口；只 flush 不 commit）。

    校验：kind 合法且非 task（占位后置，V1 不实现）；entity.subtype 在约定词表内；
    source 合法；content 非空。事实变更不走修改——新卡 + supersedes 边（内容不可变，D15）。
    """
    if kind not in CARD_KINDS:
        raise ValueError(f"非法卡片类型 kind={kind!r}；可选：{CARD_KINDS}")
    if kind == "task":
        raise NotImplementedError("task 卡占位后置（V1 不实现），不允许写入")
    if not content:
        raise ValueError("卡片正文 content 不能为空")
    if subtype is not None and kind == "entity" and subtype not in CARD_SUBTYPES_ENTITY:
        raise ValueError(f"entity.subtype={subtype!r} 非法；可选：{CARD_SUBTYPES_ENTITY}")
    if source not in CARD_SOURCES:
        raise ValueError(f"非法来源 source={source!r}；可选：{CARD_SOURCES}")
    card = Card(kind=kind, subtype=subtype, content=content, entities=entities,
                event_time=event_time, source=source,
                source_session_id=source_session_id, importance=importance)
    session.add(card)
    await session.flush()
    return card


async def get_card(session: AsyncSession, card_id: int) -> Card | None:
    """按 id 取卡（不存在返回 None）。"""
    return (await session.execute(select(Card).where(Card.id == card_id))).scalar_one_or_none()


async def list_cards(
    session: AsyncSession,
    *,
    status: str | None = ACTIVE,
    kind: str | None = None,
    subtype: str | None = None,
    source_session_id: str | None = None,
    after_id: int | None = None,
    limit: int | None = None,
    desc: bool = False,
) -> list[Card]:
    """列表查询（id 升序 + 游标分页）。

    status=None 查全部；默认只查 active（检索只认 active——D15）；
    游标分页（after_id）而非 OFFSET：快表重建/panel 翻页不随写入漂移；
    desc=True 按 id 降序——写路径取“最近卡”做候选筛选用（E8）。
    """
    stmt = select(Card).order_by(Card.id.desc() if desc else Card.id)
    if status is not None:
        stmt = stmt.where(Card.status == status)
    if kind is not None:
        stmt = stmt.where(Card.kind == kind)
    if subtype is not None:
        stmt = stmt.where(Card.subtype == subtype)
    if source_session_id is not None:
        stmt = stmt.where(Card.source_session_id == source_session_id)
    if after_id is not None:
        stmt = stmt.where(Card.id > after_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def revoke_card(session: AsyncSession, card_id: int) -> bool:
    """用户撤销（软删）：status → revoked。终态：不走版本链，rebuild_status 不动它。

    只 flush 不 commit；幂等（已是 revoked 返回 False）；返回是否发生变更。
    边不动（E11 定稿）：撤销不自动剪断相关边——检索面由“只认 active”兜住；
    剪断 = 用户手动操作（cut_edge）或版本回滚的内部机制（rollback_card）。
    向量面同步：调用方在 commit_and_sync(delete_card_ids=[card_id]) 里带出。
    """
    card = await get_card(session, card_id)
    if card is None:
        raise ValueError(f"卡片不存在：{card_id}")
    if card.status == REVOKED:
        return False
    card.status = REVOKED
    await session.flush()
    return True


async def bump_access(session: AsyncSession, card_id: int) -> None:
    """访问统计 +1（last_accessed_at 置当前 UTC；D15：证据字段，只笔不改语义）。

    卡不存在时静默无操作（update rowcount=0）；只 flush 不 commit。
    """
    await session.execute(
        update(Card)
        .where(Card.id == card_id)
        .values(access_count=Card.access_count + 1,
                last_accessed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()


# —— 边 ——

async def create_edge(
    session: AsyncSession,
    *,
    from_card_id: int,
    to_card_id: int,
    kind: str,
    source_session_id: str | None = None,
) -> Edge:
    """连线（只 flush 不 commit）。

    校验：kind 合法且非 supersedes（取代必须走 supersede_card 的同事务两语句）；
    禁止自环；两端卡必须真实存在（SQLite 外键默认不强制——写入口兜底，数据库文档 §6）。
    """
    if kind not in EDGE_KINDS:
        raise ValueError(f"非法边类型 kind={kind!r}；可选：{EDGE_KINDS}")
    if kind == KIND_SUPERSEDES:
        raise ValueError("supersedes 必须走 supersede_card()（同事务插边+改状态，铁律见工程规范 §2）")
    if from_card_id == to_card_id:
        raise ValueError("禁止自环边（同一张卡两端）")
    for card_id in (from_card_id, to_card_id):
        if await get_card(session, card_id) is None:
            raise ValueError(f"边指向不存在的卡片：{card_id}")
    edge = Edge(from_card_id=from_card_id, to_card_id=to_card_id, kind=kind,
                source_session_id=source_session_id)
    session.add(edge)
    await session.flush()
    return edge


async def get_edge(session: AsyncSession, edge_id: int) -> Edge | None:
    """按 id 取边（不存在返回 None）。"""
    return (await session.execute(select(Edge).where(Edge.id == edge_id))).scalar_one_or_none()


async def list_edges(
    session: AsyncSession,
    *,
    card_id: int | None = None,
    kind: str | None = None,
    direction: str = "both",
    include_cut: bool = False,
    limit: int | None = None,
) -> list[Edge]:
    """查边：direction = from（出边）/ to（入边）/ both。

    例（数据库文档 §5）：某事件的起因 = card_id=本卡, kind="caused_by", direction="from"；
    反查“引发了什么” = 同参但 direction="to"。
    include_cut=False（默认，E11）：排除已剪断边（cut_at 非空）——顺线/回填/版本重建
    一律吃默认值；面板审计视图可传 True 看全量。
    """
    if direction not in ("from", "to", "both"):
        raise ValueError(f"direction={direction!r} 非法；可选：from / to / both")
    stmt = select(Edge).order_by(Edge.id)
    if not include_cut:
        stmt = stmt.where(Edge.cut_at.is_(None))
    if kind is not None:
        stmt = stmt.where(Edge.kind == kind)
    if card_id is not None:
        if direction == "from":
            stmt = stmt.where(Edge.from_card_id == card_id)
        elif direction == "to":
            stmt = stmt.where(Edge.to_card_id == card_id)
        else:
            stmt = stmt.where((Edge.from_card_id == card_id) | (Edge.to_card_id == card_id))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# —— 明文（messages）——

async def append_message(
    session: AsyncSession,
    *,
    session_id: str,
    role: str,
    content: str,
) -> Message:
    """归档一条明文（只 flush 不 commit）。messages 只增不改不删（系统设计 §2.1）。

    注：主链归档（history_service）沿用自写路径（append-only，无一致性语义）；
    本函数供 E9 二级回源回填与 panel 使用，两条路径写同一张表、结构一致。
    """
    if role not in ("user", "assistant"):
        raise ValueError(f"非法角色 role={role!r}；可选：user / assistant")
    msg = Message(session_id=session_id, role=role, content=content)
    session.add(msg)
    await session.flush()
    return msg


async def list_messages(
    session: AsyncSession,
    *,
    session_id: str | None = None,
    after_id: int | None = None,
    limit: int | None = None,
) -> list[Message]:
    """列明文（id 升序 + 游标）——二级回源（E9）与 panel 的取数入口。

    session_id=None 表示**全局**（跨会话，写路径批抽取的 D23 口径）；传会话则按会话过滤。
    """
    stmt = select(Message).order_by(Message.id)
    if session_id is not None:
        stmt = stmt.where(Message.session_id == session_id)
    if after_id is not None:
        stmt = stmt.where(Message.id > after_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def _escape_like(text: str) -> str:
    """LIKE 字面量转义（反斜杠 / % / _）——关键词里的通配符不当语法用。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_messages(
    session: AsyncSession,
    *,
    keywords: Sequence[str],
    limit: int | None = None,
) -> list[Message]:
    """明文关键词模糊搜索（二级回源 L2 用；E9 定稿，2026-10-01）。

    语义：任一词命中即入选（OR LIKE，大小写不敏感）；按 id 降序（越近的原文越优先）；
    关键词去首尾空白后去重、空串跳过、%、_ 按字面量转义；空关键词直接返回 []（不发 SQL）。
    只读、无事务语义——扫描预算（limit）由调用方给（E9 传 SOURCE_SCAN_LIMIT 量级）。
    """
    kws = [k for k in dict.fromkeys(kw.strip() for kw in keywords) if k]
    if not kws:
        return []
    stmt = (
        select(Message)
        .where(or_(*[Message.content.ilike(f"%{_escape_like(kw)}%", escape="\\") for kw in kws]))
        .order_by(Message.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# —— 写路径全局统计（E8；D23 全局水位口径）——

async def get_global_message_stats(session: AsyncSession) -> tuple[int, datetime | None]:
    """全局消息统计：最新消息 id（无消息 → 0）+ 最新消息时间（无消息 → None）。

    消费方：写路径触发判断 / 启动扫描 / 空闲收口 / 滞后报告（consolidator；D23 起全局口径）。
    只读，不开事务语义——调用方自查。
    """
    row = (await session.execute(
        select(func.max(Message.id), func.max(Message.created_at))
    )).one()
    return int(row[0] or 0), row[1]


# ============================================================
# E11 编辑层原语（淘汰 / 恢复 / 剪断 / 回滚 / 硬删）
# —— 同三表 CRUD 规约：只 flush 不 commit；快表/向量同步由调用方 commit_and_sync 带出
# ============================================================

async def evict_cards(session: AsyncSession, card_ids: Sequence[int]) -> list[int]:
    """批量下架（衰减淘汰）：active → evicted；返回实际变更的卡 id 列表。

    只处理 active 卡（非 active 静默跳过——幂等，重复 sweep 安全）；只 flush 不 commit。
    同步约定：调用方在 commit_and_sync(delete_card_ids=已下架 id) 里带出——
    非 active 卡自动走删除路径（快表摘除 + Qdrant 删向量），三面一致。
    """
    ids = list(dict.fromkeys(card_ids))
    if not ids:
        return []
    active_ids = list((await session.execute(
        select(Card.id).where(Card.id.in_(ids), Card.status == ACTIVE)
    )).scalars().all())
    if not active_ids:
        return []
    await session.execute(
        update(Card)
        .where(Card.id.in_(active_ids))
        .values(status=EVICTED)
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()
    return active_ids


async def restore_card(session: AsyncSession, card_id: int) -> bool:
    """恢复下架/撤销的卡：revoked / evicted → active；返回是否发生变更。

    守卫（防链路回荡）：
    - 卡不存在 → raise；
    - active → False（无需恢复）；
    - superseded → raise（被取代的卡走 rollback_card，不走恢复）；
    - 存在未剪断的取代入边 → raise（先 rollback_card 处理版本链，再恢复）；
    - revoked / evicted 且无未剪断入边 → active，返回 True。

    只 flush 不 commit；同步约定：调用方在 commit_and_sync(upsert_cards=[card]) 带出。
    """
    card = await get_card(session, card_id)
    if card is None:
        raise ValueError(f"卡片不存在：{card_id}")
    if card.status == ACTIVE:
        return False
    if card.status == SUPERSEDED:
        raise ValueError("superseded 卡不可直接恢复——版本回退请用 rollback_card()")
    incoming = (await session.execute(
        select(func.count()).select_from(Edge)
        .where(Edge.to_card_id == card_id, Edge.kind == KIND_SUPERSEDES, Edge.cut_at.is_(None))
    )).scalar_one()
    if incoming:
        raise ValueError("该卡存在未剪断的取代入边——请先用 rollback_card() 处理版本链")
    card.status = ACTIVE
    await session.flush()
    return True


async def cut_edge(session: AsyncSession, edge_id: int) -> bool:
    """剪断边（标记式，E11 定稿）：cut_at 置当前 UTC；返回是否发生变更。

    - 仅因果边可手动剪断；supersedes 版本边 → raise（版本回退请用 rollback_card()）；
    - 幂等：已剪断返回 False；边不存在 → raise；
    - 剪断的边不参与顺线/回填/版本重建（list_edges 默认排除），仅面板审计可见。

    只 flush 不 commit；同步约定：调用方在 commit_and_sync(cut_edges=[edge]) 带出
    （快表邻接镜像摘边）。
    """
    edge = await get_edge(session, edge_id)
    if edge is None:
        raise ValueError(f"边不存在：{edge_id}")
    if edge.kind == KIND_SUPERSEDES:
        raise ValueError("版本边（supersedes）不可手动剪断——版本回退请用 rollback_card()")
    if edge.cut_at is not None:
        return False
    edge.cut_at = datetime.now(timezone.utc)
    await session.flush()
    return True


async def rollback_card(session: AsyncSession, card_id: int) -> dict[str, Any]:
    """版本回滚：目标卡回到 active，取代它的后继链全部撤销（E11 定稿算法 / D26）。

    算法：
    ① 校验目标卡为 superseded（其他状态 → raise，各走各的恢复路径）；
    ② 沿“未剪断入边 supersedes（to=当前）”收集后继链 [B, C, …]（seen 防环）；
    ③ 链上版本边全部标记剪断（cut_at=now）——不再参与版本重建；
    ④ 链上卡片全部 revoke（软删终态；已是 revoked 幂等跳过）；
    ⑤ 目标卡 status 置回 active。

    只 flush 不 commit。返回 {"restored": Card, "revoked": list[Card], "cut_edges": list[Edge]}；
    同步约定：调用方 commit_and_sync(upsert_cards=[restored], delete_card_ids=revoked id,
    cut_edges=cut_edges) 一把带出三面（快表/向量/邻接镜像）。
    """
    card = await get_card(session, card_id)
    if card is None:
        raise ValueError(f"卡片不存在：{card_id}")
    if card.status != SUPERSEDED:
        raise ValueError(f"仅 superseded 卡可回滚（当前 status={card.status}）——"
                         "撤销/下架卡请用 restore_card()")
    cut_edges: list[Edge] = []
    successors: list[Card] = []
    seen = {card.id}
    current = card
    while True:
        incoming = await list_edges(session, card_id=current.id, kind=KIND_SUPERSEDES,
                                    direction="to")
        nxt_edge = next((e for e in incoming if e.from_card_id not in seen), None)
        if nxt_edge is None:
            break
        seen.add(nxt_edge.from_card_id)
        nxt_card = await get_card(session, nxt_edge.from_card_id)
        if nxt_card is None:           # 边指向已物理删除的卡（异常态）——止步
            break
        successors.append(nxt_card)
        cut_edges.append(nxt_edge)
        current = nxt_card

    now = datetime.now(timezone.utc)
    for edge in cut_edges:
        edge.cut_at = now
    for successor in successors:
        await revoke_card(session, successor.id)   # 幂等；已是 revoked 跳过
    card.status = ACTIVE
    await session.flush()
    return {"restored": card, "revoked": successors, "cut_edges": cut_edges}


async def purge_card(session: AsyncSession, card_id: int) -> dict[str, Any]:
    """被遗忘权（硬删）：物理删除卡片 + 双向相关边（含已剪断边）；旧卡回荡。

    行为：
    ① 收集本卡未剪断 supersedes 出边指向的旧卡（本卡曾取代它们）；
    ② 删除双向相关边 + 物理删除本卡；
    ③ 回荡：旧卡若无其它未剪断取代入边 → status 置回 active（与 rebuild 真理一致，
       不留“无入边的 superseded”漂移）；revoked / evicted 的旧卡保持权威标记。

    边界声明：messages 原文层不在本原语范围（原文只增不删——被遗忘权目前只到卡层）。
    只 flush 不 commit。返回 {"card": Card, "edges_deleted": int, "restored": list[Card]}；
    同步约定：调用方 commit_and_sync(upsert_cards=restored, delete_card_ids=[card_id]) 带出。
    """
    card = await get_card(session, card_id)
    if card is None:
        raise ValueError(f"卡片不存在：{card_id}")
    out_edges = await list_edges(session, card_id=card_id, kind=KIND_SUPERSEDES,
                                 direction="from")
    old_ids = [e.to_card_id for e in out_edges]
    deleted = await session.execute(
        delete(Edge).where((Edge.from_card_id == card_id) | (Edge.to_card_id == card_id))
    )
    await session.execute(delete(Card).where(Card.id == card_id))
    restored: list[Card] = []
    for old_id in old_ids:
        remaining = (await session.execute(
            select(func.count()).select_from(Edge)
            .where(Edge.to_card_id == old_id, Edge.kind == KIND_SUPERSEDES,
                   Edge.cut_at.is_(None))
        )).scalar_one()
        if remaining:
            continue
        old_card = await get_card(session, old_id)
        if old_card is not None and old_card.status == SUPERSEDED:
            old_card.status = ACTIVE
            restored.append(old_card)
    await session.flush()
    return {"card": card, "edges_deleted": int(deleted.rowcount or 0), "restored": restored}


# ============================================================
# 写序列编排（D5）：DB 事务 → 快表（无 await 纯内存）→ Qdrant
# ============================================================

# 快表钩子（E6 pool.py 启动时注册；契约见 register_pool_hook）
_pool_hook: Callable[[str, Any], None] | None = None


def register_pool_hook(hook: Callable[[str, Any], None] | None) -> None:
    """注册快表更新钩子（E6 快表启动时调用；传 None 解除）。

    契约（D5；E6 扩展；E11 再扩展）：hook(action, payload)，必须**同步纯内存、无 await**——
    - action="upsert"：payload = list[Card]（新增/仍有效的卡，按传入顺序）；
    - action="delete"：payload = list[int]（被取代/撤销/删除/下架的卡 id）；
    - action="edge_upsert"：payload = list[(from_id, to_id, kind)]（本事务涉及的边——
      补齐"边事件不在同步轨"缺口，快表线表镜像据此同步；两端在册才装）；
    - action="edge_cut"：payload = list[(from_id, to_id, kind)]（本事务剪断的边——E11
      编辑层；快表邻接镜像据此摘边，与 edge_upsert 互逆）。
    快表失败不打断主链路（钩子内部应自保证；此处再兜一层告警）。
    """
    global _pool_hook
    _pool_hook = hook


async def commit_and_sync(
    session: AsyncSession,
    *,
    upsert_cards: Sequence[Card] = (),
    delete_card_ids: Sequence[int] = (),
    upsert_edges: Sequence[Edge] = (),
    cut_edges: Sequence[Edge] = (),
) -> None:
    """唯一写入口的编排面：提交 DB 事务，随后按序同步快表与 Qdrant（D5 写序列）。

    ① DB：所有 add/flush 过的原语（create_card / supersede_card / revoke_card…）在此一次性落定；
    ② 快表：同步纯内存调用（无 await、天然原子）——快表是下一帧读的直接来源，先于 Qdrant；
    ③ Qdrant：向量面只装 active（D17）——upsert 传入非 active 卡自动改道删除；失败不抛出，
       落重试队列（启动对账最终兜底）。“Qdrant 挂掉不影响 DB 提交”是验收铁律。

    约定：事务内新建的卡须已 flush 出 id；会话须 expire_on_commit=False（项目标准）。
    upsert_edges：本事务新建/涉及的边（supersede_card 的版本边、create_edge 的因果线）——
    随快表钩子 "edge_upsert" 同步；快表两端在册才装（E6 扩展）。
    cut_edges：本事务剪断的边（E11 cut_edge / rollback_card）——随快表钩子 "edge_cut"
    同步摘除邻接镜像（与 edge_upsert 互逆）。
    """
    upserts: list[Card] = []
    deletes: list[int] = list(dict.fromkeys(delete_card_ids))
    for card in upsert_cards:
        if card.id is None:
            raise ValueError("卡片尚未 flush（无 id）——请先经 create_card 落库再提交")
        if card.status == ACTIVE:
            upserts.append(card)
        elif card.id not in deletes:
            deletes.append(card.id)

    # 提交前抓取向量面所需数据（不依赖提交后对象状态）
    upsert_data = [(c.id, c.content, _card_payload(c)) for c in upserts]
    edge_events = [(e.from_card_id, e.to_card_id, e.kind) for e in upsert_edges]
    cut_events = [(e.from_card_id, e.to_card_id, e.kind) for e in cut_edges]

    await session.commit()                                    # ① DB（唯一提交点）

    if _pool_hook is not None:                                # ② 快表（无 await）
        try:
            if upserts:
                _pool_hook("upsert", upserts)
            if deletes:
                _pool_hook("delete", deletes)
            if edge_events:
                _pool_hook("edge_upsert", edge_events)
            if cut_events:
                _pool_hook("edge_cut", cut_events)
        except Exception as e:                                # noqa: BLE001 —— 不阻断回复
            logger.warning(f"快表更新失败（不影响 DB 与回复）: {e}")

    await _sync_vectors(upsert_data, deletes)                 # ③ Qdrant（失败→重试队列）


async def _sync_vectors(upsert_data: list[tuple[int, str, dict[str, Any]]],
                        delete_ids: list[int]) -> None:
    """向量面增量同步（失败降级：不抛异常，整批落重试队列）。"""
    if not settings.QDRANT_ENABLED:
        return
    ops: list[tuple[str, Any]] = []
    if upsert_data:
        ops.append(("upsert", upsert_data))
    if delete_ids:
        ops.append(("delete", list(delete_ids)))
    if not ops:
        return
    try:
        await _run_vector_ops(ops)
    except Exception as e:                                    # noqa: BLE001 —— 故障注入验收
        _retry_queue.extend(ops)
        logger.warning(f"Qdrant 写入失败，已入重试队列（启动对账/下次 flush 补齐）: {e}")


# ============================================================
# 一致性检查（E5）：启动时探测漂移 → 告警 + 物化视图重建
# ============================================================

async def check_and_rebuild_status(session: AsyncSession, *, commit: bool = True) -> dict[str, int]:
    """一致性检查：探测 status 与 edges 的漂移；有漂移 → 告警 + rebuild_status 修复。

    返回 {"drift": 探测到的漂移条数, "fixed": 实际修正条数}。幂等（无漂移时 fixed=0）。
    与 rebuild_status 的关系：本函数 = 探测（报警）+ 修复 + 提交 的组合面（启动钩子用）。
    """
    incoming = select(Edge.to_card_id).where(
        Edge.kind == KIND_SUPERSEDES, Edge.cut_at.is_(None))   # E11：剪断边不参与推导
    drift_torn = (await session.execute(          # 该 superseded 却仍是 active（插边未改状态）
        select(func.count()).select_from(Card)
        .where(Card.status.notin_([SUPERSEDED, REVOKED, EVICTED]), Card.id.in_(incoming))
    )).scalar_one()
    drift_ghost = (await session.execute(         # 标着 superseded 却无入边（边被剪断/回滚）
        select(func.count()).select_from(Card)
        .where(Card.status == SUPERSEDED, Card.id.notin_(incoming))
    )).scalar_one()
    drift = int(drift_torn) + int(drift_ghost)
    fixed = 0
    if drift:
        logger.warning(f"一致性漂移：{drift} 张卡 status 与 edges 不符，自动重建…")
        fixed = await rebuild_status(session)
    if commit:
        await session.commit()
    return {"drift": drift, "fixed": fixed}


# ============================================================
# Qdrant 向量面封装（唯一入口；D17：集合 = “active 卡集合”的向量镜像）
# ============================================================

# Qdrant 失败重试队列（进程内存；崩溃丢失由启动对账兜底——终态一致不依赖队列）
_retry_queue: list[tuple[str, Any]] = []
# Qdrant 封装单例（按 settings 懒建；测试 monkeypatch get_qdrant）
_qdrant_index: "QdrantIndex | None" = None


def _card_payload(card: Card) -> dict[str, Any]:
    """点 payload：最小可用集（type 为对账/检索的过滤键；kind/subtype 供观测）。"""
    return {"type": "card", "kind": card.kind, "subtype": card.subtype}


class QdrantIndex:
    """Qdrant 薄封装：懒连接 + 幂等建集合 + 卡点增删查。

    不暴露给记忆包外部；上层经 store 模块函数使用（get_qdrant / commit_and_sync / reconcile）。
    """

    def __init__(self, url: str, collection: str, dim: int, timeout: float):
        self._url, self._collection = url, collection
        self._dim, self._timeout = dim, timeout
        self._client = None

    @property
    def collection(self) -> str:
        return self._collection

    def _c(self):
        if self._client is None:
            from qdrant_client import QdrantClient          # 懒导入：纯 CRUD 用法不依赖
            self._client = QdrantClient(url=self._url, timeout=self._timeout)
        return self._client

    def ensure_collection(self) -> None:
        """幂等建集合（不存在才建；维度/距离按配置）。"""
        from qdrant_client.models import Distance, VectorParams
        if not self._c().collection_exists(self._collection):
            self._c().create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
            )

    def upsert_points(self, points: list[tuple[int, list[float], dict[str, Any]]]) -> None:
        """批量写点：id = 卡片 id（统一 ID 空间，串链/回源零映射）。"""
        from qdrant_client.models import PointStruct
        self._c().upsert(
            collection_name=self._collection,
            points=[PointStruct(id=pid, vector=vec, payload=meta) for pid, vec, meta in points],
            wait=True,
        )

    def delete_ids(self, ids: list[int]) -> None:
        """删点（取代/撤销后旧向量即删——防旧卡占名额，D17）。"""
        from qdrant_client.models import PointIdsList
        if not ids:
            return
        self._c().delete(collection_name=self._collection,
                         points_selector=PointIdsList(points=list(ids)), wait=True)

    def card_ids(self) -> set[int]:
        """集合内全部卡点 id（scroll 全量；对账用）。"""
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        flt = Filter(must=[FieldCondition(key="type", match=MatchValue(value="card"))])
        ids: set[int] = set()
        offset = None
        while True:
            records, offset = self._c().scroll(
                collection_name=self._collection, scroll_filter=flt,
                limit=1000, offset=offset, with_payload=False, with_vectors=False,
            )
            ids.update(int(r.id) for r in records)
            if offset is None:
                break
        return ids

    def search(self, vector: list[float], *, limit: int = 10) -> list[tuple[int, float]]:
        """语义 top-k（E9 retriever 的向量入口）——集合只装 active，无需状态过滤。"""
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        resp = self._c().query_points(
            collection_name=self._collection, query=vector, limit=limit,
            query_filter=Filter(must=[FieldCondition(key="type", match=MatchValue(value="card"))]),
        )
        return [(int(p.id), float(p.score)) for p in resp.points]


def get_qdrant() -> QdrantIndex:
    """Qdrant 封装单例（按 settings 懒建；测试可 monkeypatch 本函数或 _qdrant_index）。"""
    global _qdrant_index
    if _qdrant_index is None:
        _qdrant_index = QdrantIndex(settings.QDRANT_URL, settings.QDRANT_COLLECTION,
                                    settings.EMBEDDING_DIM, settings.QDRANT_TIMEOUT)
    return _qdrant_index


async def _run_vector_ops(ops: Iterable[tuple[str, Any]]) -> None:
    """真正执行向量操作（直写与重试 flush 共用）；失败向上抛（由调用方降级）。

    嵌入失败也在内（重试时重新嵌入）；删除幂等。
    """
    index = get_qdrant()
    index.ensure_collection()
    for action, payload in ops:
        if action == "upsert":
            vectors = await embed_texts([text for _, text, _ in payload])
            index.upsert_points([(cid, vec, meta)
                                 for (cid, _, meta), vec in zip(payload, vectors)])
        elif action == "delete":
            index.delete_ids(list(payload))
        else:
            raise ValueError(f"未知向量操作：{action!r}")


async def flush_retry_queue() -> dict[str, int]:
    """冲刷重试队列（Qdrant 恢复后调用；启动对账内先调）。返回 {flushed, pending}。

    失败的条目重新入队、不抛出——队列是尽力而为的加速器，终态一致由对账保证。
    """
    if not _retry_queue:
        return {"flushed": 0, "pending": 0}
    pending = list(_retry_queue)
    _retry_queue.clear()
    flushed = 0
    for op in pending:
        try:
            await _run_vector_ops([op])
            flushed += 1
        except Exception as e:                                # noqa: BLE001
            _retry_queue.append(op)
            logger.warning(f"重试仍失败，保留在队列: {e}")
    return {"flushed": flushed, "pending": len(_retry_queue)}


def pending_vector_ops() -> int:
    """当前重试队列长度（测试与观测）。"""
    return len(_retry_queue)


async def reconcile_qdrant(session: AsyncSession) -> dict[str, int]:
    """启动对账：让 Qdrant 与“DB 的 active 卡集合”完全一致（D17 不变量）。

    ① 先冲重试队列；② 缺 → 嵌入后补写；③ 多（旧版残留/脏数据）→ 删除。
    Qdrant 不可达时向上抛（由 startup_check 降级为告警，不阻塞启动）。
    """
    flushed = await flush_retry_queue()
    active = list((await session.execute(
        select(Card).where(Card.status == ACTIVE)
    )).scalars().all())
    db_ids = {c.id for c in active}
    index = get_qdrant()
    index.ensure_collection()
    qd_ids = index.card_ids()
    missing = [c for c in active if c.id not in qd_ids]
    extra = sorted(qd_ids - db_ids)
    if missing:
        vectors = await embed_texts([c.content for c in missing])
        index.upsert_points([(c.id, vec, _card_payload(c)) for c, vec in zip(missing, vectors)])
    if extra:
        index.delete_ids(extra)
    return {"upserted": len(missing), "deleted": len(extra),
            "flushed": flushed["flushed"], "pending": flushed["pending"]}


# ============================================================
# 向量化（provider 双轨：api 质量优先 / local 离线兜底；D17 冻结）
# ============================================================

# 本地模型单例（懒加载；仅 provider=local 时使用）
_local_model = None


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """文本 → 向量（含 MRL 截断 + L2 归一）。provider 由 settings.EMBEDDING_PROVIDER 决定。"""
    if not texts:
        return []
    provider = settings.EMBEDDING_PROVIDER.lower()
    if provider == "api":
        raw = await _embed_via_api(texts)
    elif provider == "local":
        raw = await _embed_via_local(texts)
    else:
        raise ValueError(f"未知 EMBEDDING_PROVIDER={settings.EMBEDDING_PROVIDER!r}；可选：api / local")
    return [_finalize_vector(v) for v in raw]


async def _embed_via_api(texts: list[str]) -> list[list[float]]:
    """OpenAI 兼容 embeddings 接口（默认硅基流动 Qwen3-Embedding-8B）。按 index 对齐返回顺序。"""
    if not settings.EMBEDDING_API_KEY:
        raise RuntimeError("EMBEDDING_API_KEY 未配置（.env）——API 向量不可用")
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=settings.EMBEDDING_BASE_URL,
                         api_key=settings.EMBEDDING_API_KEY,
                         timeout=settings.API_TIMEOUT)
    resp = await client.embeddings.create(model=settings.EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]


async def _embed_via_local(texts: list[str]) -> list[list[float]]:
    """本地 sentence-transformers（torch 栈按需安装；首次调用加载模型，线程池执行不阻塞事件循环）。"""
    def _encode() -> list[list[float]]:
        global _local_model
        if _local_model is None:
            from sentence_transformers import SentenceTransformer
            _local_model = SentenceTransformer(settings.EMBEDDING_MODEL)
        return _local_model.encode(texts, normalize_embeddings=False).tolist()
    return await asyncio.to_thread(_encode)


def _finalize_vector(vector: list[float]) -> list[float]:
    """MRL 截断到 EMBEDDING_DIM + L2 归一（Qwen3 官方支持 MRL；维度不足 = 配置与模型不符，报错）。"""
    dim = settings.EMBEDDING_DIM
    if len(vector) < dim:
        raise ValueError(f"向量维度 {len(vector)} < EMBEDDING_DIM={dim}——模型与配置不匹配")
    vec = [float(x) for x in vector[:dim]]
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return vec
    return [x / norm for x in vec]


# ============================================================
# 启动钩子（main.py lifespan 调用；所有跨模块依赖至此已定义）
# ============================================================

async def startup_check(session: AsyncSession) -> dict[str, Any]:
    """启动一致性检查 + Qdrant 对账。

    - status 漂移检查：失败向上抛（DB 层问题应当暴露）；
    - Qdrant 对账：不可达只告警不阻塞（QDRANT_ENABLED=False 时整段跳过）；
    - 未配 EMBEDDING_API_KEY 且 Qdrant 可达时，对账会在“缺卡补写”处报错——降级为告警（同不可达）。
    """
    report: dict[str, Any] = dict(await check_and_rebuild_status(session))
    report["vector"] = None
    if settings.QDRANT_ENABLED:
        try:
            report["vector"] = await reconcile_qdrant(session)
        except Exception as e:                                # noqa: BLE001 —— 启动永远不阻塞
            report["vector"] = {"error": str(e)}
            logger.warning(f"Qdrant 对账跳过（重试队列保留，恢复后补齐）: {e}")
    return report
