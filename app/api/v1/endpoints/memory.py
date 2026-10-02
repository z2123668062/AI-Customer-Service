# app/api/v1/endpoints/memory.py
"""记忆管理接口（E11 实装）：记忆面板与“编辑层”的服务端入口——前端只吃 HTTP。

对应《淘汰与编辑层文档.md》；读写一律走 store 原语 + commit_and_sync（DB/快表/向量
三面一致），本层只做参数校验、404/400 语义与审计日志。

端点清单（路由顺序纪律：字面路径必须注册在 /{card_id} 之前）：
- GET    /               列表（status/kind/subtype 过滤；desc 取最近 / asc 游标分页）
- GET    /stats          总览统计（卡计数 + 快表观测 + 最近淘汰时间）
- GET    /evict-log      淘汰日志（最新在前）
- POST   /evict          手动触发一次淘汰巡检（sweep；消融档位内自动跳过）
- DELETE /edges/{id}     剪断因果边（版本边不可手动剪——回退走 rollback）
- GET    /{id}           单卡详情（双向边含剪断标记 + 版本链）
- PATCH  /{id}           内容修订（新卡 + supersedes 旧卡，不原地改——D15）
- DELETE /{id}           撤销（软删 revoke；可 restore / purge）
- POST   /{id}/restore   恢复（revoked / evicted → active）
- POST   /{id}/rollback  版本回滚（目标卡回 active；后继链撤销 + 版本边剪断）
- DELETE /{id}/purge     被遗忘权（物理删卡 + 相关边；messages 原文保留——边界声明）
"""
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.exceptions import AppException
from app.core.logging import logger
from app.models.domain import CARD_KINDS, CARD_STATUSES, Card
from app.models.schemas import (CardDetail, CardItem, CardListResponse,
                                CardUpdateRequest, EdgeItem, EditResponse,
                                EvictLogResponse, EvictRunResponse, MemoryStats)
from app.services.long_term_memory import evictor, pool, store

router = APIRouter()


async def get_session():
    """请求级数据库会话（唯一提交点 = store.commit_and_sync；只读请求自动回滚收尾）。"""
    async with AsyncSessionLocal() as session:
        yield session


def _not_found(card_id: int) -> AppException:
    return AppException(error_code="NOT_FOUND", message=f"卡片不存在：{card_id}",
                        http_status=404, module="memory_endpoint")


def _bad_request(message: str) -> AppException:
    return AppException(error_code="BAD_REQUEST", message=message,
                        http_status=400, module="memory_endpoint")


# ============================================================
# 编辑审计日志（EDIT_LOG_PATH；fail-open）
# ============================================================

def _edit_log_path() -> str:
    p = settings.EDIT_LOG_PATH
    return p if os.path.isabs(p) else os.path.join(settings.BASE_DIR, p)


def _log_edit(action: str, **fields) -> None:
    """编辑操作审计（fail-open：写失败只告警——审计可丢，操作不回退）。"""
    record = {"ts": datetime.now(timezone.utc).isoformat(), "action": action, **fields}
    try:
        path = _edit_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 —— fail-open 是本模块的硬约定
        logger.warning(f"编辑日志写盘失败（已忽略）: {exc}")


# ============================================================
# 列表 / 统计 / 淘汰日志 / 手动巡检（字面路径——注册在 /{card_id} 之前）
# ============================================================

@router.get("", response_model=CardListResponse)
async def list_memory(
    status: str | None = Query(default=None, description="active/superseded/revoked/evicted；不传=全量"),
    kind: str | None = Query(default=None, description="entity / event"),
    subtype: str | None = Query(default=None, description="identity/preference/goal/relation"),
    source_session_id: str | None = Query(default=None, description="按来源会话过滤"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    after_id: int | None = Query(default=None, description="asc 游标：从该 id 之后取；desc 忽略"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """记忆列表：默认最新在前（desc，面板用）；asc 支持游标分页（大数据量遍历用）。"""
    if status is not None and status not in CARD_STATUSES:
        raise _bad_request(f"status={status!r} 非法；可选：{list(CARD_STATUSES)}")
    if kind is not None and kind not in CARD_KINDS:
        raise _bad_request(f"kind={kind!r} 非法；可选：{list(CARD_KINDS)}")
    desc = order == "desc"
    cards = await store.list_cards(
        session, status=status, kind=kind, subtype=subtype,
        source_session_id=source_session_id,
        after_id=None if desc else after_id, limit=limit, desc=desc,
    )
    next_after_id = None
    if not desc and len(cards) == limit:
        next_after_id = cards[-1].id
    return CardListResponse(
        items=[CardItem.model_validate(c) for c in cards],
        count=len(cards),
        next_after_id=next_after_id,
    )


@router.get("/stats", response_model=MemoryStats)
async def memory_stats(session: AsyncSession = Depends(get_session)):
    """总览：卡计数（按状态/类型）+ 快表观测 + 最近淘汰巡检时间。"""
    total = (await session.execute(select(func.count()).select_from(Card))).scalar_one()
    by_status = {s: int(c) for s, c in (await session.execute(
        select(Card.status, func.count()).group_by(Card.status))).all()}
    by_kind = {k: int(c) for k, c in (await session.execute(
        select(Card.kind, func.count()).group_by(Card.kind))).all()}
    last_log = evictor.read_evict_log(limit=1)
    return MemoryStats(
        total=int(total),
        by_status=by_status,
        by_kind=by_kind,
        pool=pool.stats(),
        last_evict_ts=last_log[0].get("ts") if last_log else None,
    )


@router.get("/evict-log", response_model=EvictLogResponse)
async def get_evict_log(limit: int = Query(default=50, ge=1, le=200)):
    """淘汰日志（最新在前）：面板“淘汰日志”区的数据源。"""
    return EvictLogResponse(items=evictor.read_evict_log(limit))


@router.post("/evict", response_model=EvictRunResponse)
async def run_evict(session: AsyncSession = Depends(get_session)):
    """手动触发一次淘汰巡检（确定性规则；消融档位 EVICT_ENABLED=False 时自动跳过）。"""
    report = await evictor.sweep(session)
    _log_edit("evict", scanned=report["scanned"], evicted=report["evicted"])
    return EvictRunResponse(**report)


@router.delete("/edges/{edge_id}", response_model=EditResponse)
async def cut_memory_edge(edge_id: int, session: AsyncSession = Depends(get_session)):
    """剪断因果边（标记式）：剪断后不参与顺线/回填/版本重建，仅审计可见。

    版本边（supersedes）拒绝——版本回退请走 POST /{id}/rollback。
    """
    try:
        changed = await store.cut_edge(session, edge_id)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    if changed:
        edge = await store.get_edge(session, edge_id)
        await store.commit_and_sync(session, cut_edges=[edge])
    _log_edit("cut_edge", edge_id=edge_id, changed=changed)
    return EditResponse(action="cut_edge", cut_edge_ids=[edge_id],
                        detail="已剪断" if changed else "该边已处于剪断态（幂等）")


# ============================================================
# 单卡：详情 / 修订 / 撤销 / 恢复 / 回滚 / 硬删
# ============================================================

async def _collect_versions(session: AsyncSession, start_id: int) -> list[Card]:
    """沿出边 supersedes 收集旧版本链（近→远；seen 防环）。"""
    versions: list[Card] = []
    seen = {start_id}
    current = start_id
    while True:
        edges = await store.list_edges(session, card_id=current,
                                       kind=store.KIND_SUPERSEDES, direction="from")
        nxt = next((e for e in edges if e.to_card_id not in seen), None)
        if nxt is None:
            break
        seen.add(nxt.to_card_id)
        card = await store.get_card(session, nxt.to_card_id)
        if card is None:
            break
        versions.append(card)
        current = card.id
    return versions


@router.get("/{card_id}", response_model=CardDetail)
async def get_memory(card_id: int, session: AsyncSession = Depends(get_session)):
    """单卡详情：卡 + 双向相关边（含剪断标记）+ 版本历史链 + 当前取代者。"""
    card = await store.get_card(session, card_id)
    if card is None:
        raise _not_found(card_id)
    edges = await store.list_edges(session, card_id=card_id, include_cut=True)
    superseded_by = next(
        (e.from_card_id for e in edges
         if e.kind == store.KIND_SUPERSEDES and e.to_card_id == card_id and e.cut_at is None),
        None,
    )
    versions = await _collect_versions(session, card_id)
    return CardDetail(
        **CardItem.model_validate(card).model_dump(),
        edges=[EdgeItem.model_validate(e) for e in edges],
        superseded_by=superseded_by,
        versions=[CardItem.model_validate(c) for c in versions],
    )


@router.patch("/{card_id}", response_model=EditResponse)
async def edit_memory(card_id: int, body: CardUpdateRequest,
                      session: AsyncSession = Depends(get_session)):
    """内容修订：新卡接棒（继承 kind/subtype/entities 等结构），版本边指向旧卡。

    不原地改（D15）：内容不可变，修订 = 新版本 + supersedes；旧卡自动 superseded。
    """
    old = await store.get_card(session, card_id)
    if old is None:
        raise _not_found(card_id)
    if old.status != store.ACTIVE:
        raise _bad_request(f"仅 active 卡可修订（当前 {old.status}）——先 restore 或 rollback")
    if body.content == old.content:
        raise _bad_request("修订内容与原卡一致——未生成新版本")
    new_card = await store.create_card(
        session,
        kind=old.kind, subtype=old.subtype, content=body.content,
        entities=list(old.entities) if old.entities else None,
        event_time=old.event_time, source=old.source,
        source_session_id=old.source_session_id, importance=old.importance,
    )
    edge = await store.supersede_card(session, new_card_id=new_card.id, old_card_id=old.id)
    await store.commit_and_sync(session, upsert_cards=[new_card], upsert_edges=[edge])
    _log_edit("edit", card_id=card_id, new_card_id=new_card.id)
    return EditResponse(action="edit", card_id=card_id, new_card_id=new_card.id,
                        detail=f"已生成新版本 #{new_card.id}，旧版本自动 superseded")


@router.delete("/{card_id}", response_model=EditResponse)
async def delete_memory(card_id: int, session: AsyncSession = Depends(get_session)):
    """撤销（软删）：status → revoked（终态）；可 restore 恢复，可 purge 硬删。"""
    card = await store.get_card(session, card_id)
    if card is None:
        raise _not_found(card_id)
    changed = await store.revoke_card(session, card_id)
    if changed:
        await store.commit_and_sync(session, delete_card_ids=[card_id])
    _log_edit("delete", card_id=card_id, changed=changed)
    return EditResponse(action="delete", card_id=card_id,
                        detail="已撤销（软删；可 restore 恢复）" if changed
                        else "已是 revoked（幂等）")


@router.post("/{card_id}/restore", response_model=EditResponse)
async def restore_memory(card_id: int, session: AsyncSession = Depends(get_session)):
    """恢复：revoked / evicted → active；被取代的卡请走 rollback。"""
    if await store.get_card(session, card_id) is None:
        raise _not_found(card_id)
    try:
        changed = await store.restore_card(session, card_id)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    if changed:
        card = await store.get_card(session, card_id)
        await store.commit_and_sync(session, upsert_cards=[card])
    _log_edit("restore", card_id=card_id, changed=changed)
    return EditResponse(action="restore", card_id=card_id,
                        detail="已恢复为 active" if changed else "无需恢复（保持原状态）")


@router.post("/{card_id}/rollback", response_model=EditResponse)
async def rollback_memory(card_id: int, session: AsyncSession = Depends(get_session)):
    """版本回滚：目标卡回到 active；取代它的后继链全部撤销 + 版本边剪断（D26 算法）。"""
    if await store.get_card(session, card_id) is None:
        raise _not_found(card_id)
    try:
        result = await store.rollback_card(session, card_id)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    await store.commit_and_sync(
        session,
        upsert_cards=[result["restored"]],
        delete_card_ids=[c.id for c in result["revoked"]],
        cut_edges=result["cut_edges"],
    )
    revoked_ids = [c.id for c in result["revoked"]]
    cut_ids = [e.id for e in result["cut_edges"]]
    _log_edit("rollback", card_id=card_id, revoked=revoked_ids, cut_edges=cut_ids)
    return EditResponse(action="rollback", card_id=card_id,
                        affected_card_ids=revoked_ids, cut_edge_ids=cut_ids,
                        detail=(f"已恢复 #{card_id}；撤销后继 {len(revoked_ids)} 张，"
                                f"剪断版本边 {len(cut_ids)} 条"))


@router.delete("/{card_id}/purge", response_model=EditResponse)
async def purge_memory(card_id: int, session: AsyncSession = Depends(get_session)):
    """被遗忘权（硬删）：物理删除卡片 + 双向相关边（含剪断边）。

    边界声明：messages 原文层保留（原文只增不删——被遗忘权目前只到卡层）。
    """
    if await store.get_card(session, card_id) is None:
        raise _not_found(card_id)
    try:
        result = await store.purge_card(session, card_id)
    except ValueError as exc:
        raise _not_found(card_id) from exc
    await store.commit_and_sync(
        session,
        upsert_cards=result["restored"],
        delete_card_ids=[card_id],
    )
    restored_ids = [c.id for c in result["restored"]]
    _log_edit("purge", card_id=card_id, edges_deleted=result["edges_deleted"],
              restored=restored_ids)
    return EditResponse(action="purge", card_id=card_id, affected_card_ids=restored_ids,
                        detail=(f"已物理删除（连带边 {result['edges_deleted']} 条；"
                                f"{len(restored_ids)} 张旧版本回荡 active）；原文保留"))
