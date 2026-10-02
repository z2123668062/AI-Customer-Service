"""数据层 schema 单测（E2；D23 回退三表）：业务三张表建表、字段可读写、旧 memory 表退役。"""
import pytest
from sqlalchemy import inspect, select

from app.models.domain import Card, Edge, Message


@pytest.mark.asyncio
async def test_schema_has_expected_tables(db_session):
    """建表 = 业务三张（cards / edges / messages）；旧 memory 表退役，辅助状态表已随 D23 删除。"""

    def _table_names(sync_session):
        return inspect(sync_session.get_bind()).get_table_names()

    tables = await db_session.run_sync(_table_names)
    assert set(tables) == {"cards", "edges", "messages"}


@pytest.mark.asyncio
async def test_card_roundtrip_with_entities_json(db_session):
    """卡片核心字段（含 entities JSON 列）能写能读；默认值符合设计。"""
    card = Card(
        kind="entity",
        subtype="identity",
        content="用户住在上海",
        entities=["上海"],
        source_session_id="sess-1",
    )
    db_session.add(card)
    await db_session.commit()

    got = (await db_session.execute(select(Card).where(Card.id == card.id))).scalar_one()
    assert got.kind == "entity"
    assert got.entities == ["上海"]          # JSON 列还原为列表
    assert got.status == "active"            # 默认值：新卡有效
    assert got.source == "passive"           # 默认值：被动抽取
    assert got.access_count == 0
    assert got.event_time is None            # 实体卡不留事件时间


@pytest.mark.asyncio
async def test_message_and_edge_roundtrip(db_session):
    """messages 结构沿用；edges 两端指向卡片字段可读写、可反查（反查列即结果入口）。"""
    db_session.add(Message(session_id="sess-1", role="user", content="你好"))
    # caused_by 方向约定：from=结果事件、to=原因事件（"A caused_by B" = A 由 B 引起）
    c_result = Card(kind="event", content="用户去杭州出差了")
    c_cause = Card(kind="event", content="用户接到出差通知")
    db_session.add_all([c_result, c_cause])
    await db_session.commit()

    db_session.add(Edge(from_card_id=c_result.id, to_card_id=c_cause.id,
                        kind="caused_by", source_session_id="sess-1"))
    await db_session.commit()

    # 反查：某事件引发了什么 → 按 to_card_id 查该事件的所有边
    edge = (await db_session.execute(
        select(Edge).where(Edge.kind == "caused_by", Edge.to_card_id == c_cause.id)
    )).scalar_one()
    assert (edge.from_card_id, edge.to_card_id) == (c_result.id, c_cause.id)

    msg = (await db_session.execute(select(Message))).scalar_one()
    assert (msg.session_id, msg.role, msg.content) == ("sess-1", "user", "你好")
