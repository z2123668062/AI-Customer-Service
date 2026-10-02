"""store 一致性核心单测（E2）："取代 = 同事务插边 + 改状态"；rebuild_status 从边重算。"""
import pytest
from sqlalchemy import func, select

from app.models.domain import Card, Edge
from app.services.long_term_memory.store import rebuild_status, supersede_card


async def _new_card(session, content="卡片", kind="entity", status=None):
    """造一张卡并 flush 拿 id；status 传 None 走默认（active）。"""
    card = Card(kind=kind, content=content)
    if status is not None:
        card.status = status
    session.add(card)
    await session.flush()
    return card


@pytest.mark.asyncio
async def test_supersede_same_transaction_commit(db_session):
    """提交路径：插边 + 旧卡标 superseded，一次动作两条语句同时生效。"""
    old = await _new_card(db_session, "用户住在上海")
    new = await _new_card(db_session, "用户搬到北京了")
    edge = await supersede_card(db_session, new_card_id=new.id, old_card_id=old.id,
                                source_session_id="sess-1")
    await db_session.commit()

    assert old.status == "superseded"                     # 旧卡被标取代
    assert (edge.kind, edge.from_card_id, edge.to_card_id) == ("supersedes", new.id, old.id)
    assert new.status == "active"                         # 新卡保持有效
    # DB 落盘核对
    assert (await db_session.execute(
        select(func.count()).select_from(Edge).where(Edge.kind == "supersedes")
    )).scalar_one() == 1


@pytest.mark.asyncio
async def test_supersede_rollback_keeps_nothing(db_session):
    """回滚路径：边与状态同进退——不许出现"边写了但状态没改"的撕裂。"""
    old = await _new_card(db_session, "用户住在上海")
    new = await _new_card(db_session, "用户搬到北京了")
    await db_session.commit()                             # 卡片先落库，动作单独成事务
    old_id, new_id = old.id, new.id                       # 先记 id：rollback() 后对象会过期，再访问会触发隐式 IO

    await supersede_card(db_session, new_card_id=new_id, old_card_id=old_id)
    await db_session.rollback()                           # 动作整体回滚

    fresh_old = (await db_session.execute(select(Card).where(Card.id == old_id))).scalar_one()
    assert fresh_old.status == "active"                   # 状态未变
    assert (await db_session.execute(
        select(func.count()).select_from(Edge)
    )).scalar_one() == 0                                  # 边未留痕


@pytest.mark.asyncio
async def test_rebuild_status_from_edges(db_session):
    """rebuild 三规则：有入边→superseded；无入边误标→回荡 active；revoked 保持不动。"""
    # ① 撕裂态：边在、状态却还是 active（模拟"插了边没改状态"）
    torn_old = await _new_card(db_session, "旧地址")
    torn_new = await _new_card(db_session, "新地址")
    db_session.add(Edge(from_card_id=torn_new.id, to_card_id=torn_old.id, kind="supersedes"))
    # ② 幽灵态：标了 superseded、但边其实不在（模拟边被剪断）
    ghost = await _new_card(db_session, "幽灵旧卡", status="superseded")
    # ③ 撤销卡：用户软删不走版本链——即使有入边也不该被动
    revoked_old = await _new_card(db_session, "被撤销的卡", status="revoked")
    revoked_new = await _new_card(db_session, "取代撤销卡的卡")
    db_session.add(Edge(from_card_id=revoked_new.id, to_card_id=revoked_old.id, kind="supersedes"))
    await db_session.commit()

    fixed = await rebuild_status(db_session)
    await db_session.commit()

    assert fixed == 2                                     # ① 与 ② 各修一条
    assert torn_old.status == "superseded"
    assert ghost.status == "active"
    assert revoked_old.status == "revoked"                # 撤销不走版本链，保持不动
    assert (await rebuild_status(db_session)) == 0        # 修完即幂等
    await db_session.commit()


@pytest.mark.asyncio
async def test_supersede_chain_versions(db_session):
    """版本链 v1←v2←v3：仅最新版 active；rebuild 零修正（写入路径已保证一致）。"""
    v1 = await _new_card(db_session, "第一版：打算住上海")
    v2 = await _new_card(db_session, "第二版：改住北京")
    v3 = await _new_card(db_session, "第三版：落户杭州")
    await supersede_card(db_session, new_card_id=v2.id, old_card_id=v1.id)
    await supersede_card(db_session, new_card_id=v3.id, old_card_id=v2.id)
    await db_session.commit()

    assert (v1.status, v2.status, v3.status) == ("superseded", "superseded", "active")
    assert (await rebuild_status(db_session)) == 0
    await db_session.commit()
