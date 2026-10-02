"""store 存储封装单测（E5）：三表 CRUD / 写序列（DB→快表→Qdrant）/ 故障降级 / 启动对账。

策略：Qdrant 与 embedding 用内存假件替换（真 Qdrant 联调另跑 Docker 冒烟脚本）；
快表用假钩子捕获（验证顺序：快表先于 Qdrant、钩子为同步调用）。
"""
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.domain import Card, Edge
from app.services.long_term_memory import store


class FakeQdrant:
    """内存版 QdrantIndex 同接口假件；fail=True 模拟服务不可达。"""

    def __init__(self):
        self.points: dict[int, dict] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("qdrant down (injected)")

    def ensure_collection(self):
        self._check()

    def upsert_points(self, points):
        self._check()
        for pid, vec, meta in points:
            self.points[pid] = {"vector": list(vec), "payload": meta}

    def delete_ids(self, ids):
        self._check()
        for pid in ids:
            self.points.pop(pid, None)

    def card_ids(self):
        self._check()
        return {pid for pid, p in self.points.items() if p["payload"].get("type") == "card"}

    def search(self, vector, *, limit=10):
        self._check()
        return [(pid, 1.0) for pid in list(self.points)[:limit]]


@pytest.fixture
def vector_stack(monkeypatch):
    """假 Qdrant + 假 embedding + 干净重试队列/快表钩子（显式声明使用）。"""
    fake = FakeQdrant()
    monkeypatch.setattr(store, "get_qdrant", lambda: fake)
    monkeypatch.setattr(store, "_qdrant_index", None)

    async def fake_embed(texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(store, "embed_texts", fake_embed)
    store._retry_queue.clear()
    store.register_pool_hook(None)
    yield fake
    store._retry_queue.clear()
    store.register_pool_hook(None)


async def _card(session, content="卡片", kind="entity", **kw):
    return await store.create_card(session, kind=kind, content=content, **kw)


# ================================
# 卡片 CRUD
# ================================

@pytest.mark.asyncio
async def test_create_card_defaults_and_validation(db_session):
    card = await store.create_card(db_session, kind="entity", subtype="identity",
                                   content="用户住在上海", entities=["上海"])
    await db_session.commit()
    assert card.id is not None
    assert (card.status, card.source, card.access_count) == ("active", "passive", 0)

    with pytest.raises(ValueError):
        await store.create_card(db_session, kind="place", content="x")
    with pytest.raises(NotImplementedError):
        await store.create_card(db_session, kind="task", content="x")
    with pytest.raises(ValueError):
        await store.create_card(db_session, kind="entity", content="x", subtype="乱写")
    with pytest.raises(ValueError):
        await store.create_card(db_session, kind="entity", content="x", source="chat")
    with pytest.raises(ValueError):
        await store.create_card(db_session, kind="entity", content="")


@pytest.mark.asyncio
async def test_card_get_and_list_cursor(db_session):
    c1 = await _card(db_session, "一")
    c2 = await _card(db_session, "二")
    c3 = await _card(db_session, "三", kind="event", subtype="action")
    await db_session.commit()

    assert (await store.get_card(db_session, c1.id)).content == "一"
    assert await store.get_card(db_session, 99999) is None

    assert [c.id for c in await store.list_cards(db_session)] == [c1.id, c2.id, c3.id]
    assert [c.id for c in await store.list_cards(db_session, kind="event")] == [c3.id]
    assert len(await store.list_cards(db_session, status=None)) == 3

    page1 = await store.list_cards(db_session, limit=2)                 # 游标分页
    assert [c.id for c in page1] == [c1.id, c2.id]
    page2 = await store.list_cards(db_session, after_id=page1[-1].id, limit=2)
    assert [c.id for c in page2] == [c3.id]


@pytest.mark.asyncio
async def test_revoke_card_idempotent_and_rebuild_safe(db_session):
    card = await _card(db_session, "要被删的卡")
    await db_session.commit()

    assert await store.revoke_card(db_session, card.id) is True
    await db_session.commit()
    assert card.status == "revoked"

    assert await store.revoke_card(db_session, card.id) is False        # 幂等
    assert await store.rebuild_status(db_session) == 0                  # 撤销是终态，rebuild 不动
    await db_session.commit()

    assert [c.id for c in await store.list_cards(db_session)] == []     # 默认只列 active
    assert [c.id for c in await store.list_cards(db_session, status="revoked")] == [card.id]

    with pytest.raises(ValueError):
        await store.revoke_card(db_session, 99999)


@pytest.mark.asyncio
async def test_bump_access(db_session):
    card = await _card(db_session, "被访问的卡")
    await db_session.commit()
    assert card.last_accessed_at is None

    await store.bump_access(db_session, card.id)
    await store.bump_access(db_session, card.id)
    await db_session.commit()
    await db_session.refresh(card)
    assert card.access_count == 2
    assert card.last_accessed_at is not None

    await store.bump_access(db_session, 99999)                          # 不存在：静默无操作
    await db_session.commit()


# ================================
# 边 CRUD
# ================================

@pytest.mark.asyncio
async def test_create_edge_validation_and_list(db_session):
    a = await _card(db_session, "原因事件", kind="event")
    b = await _card(db_session, "结果事件", kind="event")
    e = await _card(db_session, "实体卡")
    await db_session.commit()

    with pytest.raises(ValueError):
        await store.create_edge(db_session, from_card_id=a.id, to_card_id=a.id, kind="caused_by")
    with pytest.raises(ValueError):
        await store.create_edge(db_session, from_card_id=a.id, to_card_id=b.id, kind="supersedes")
    with pytest.raises(ValueError):
        await store.create_edge(db_session, from_card_id=a.id, to_card_id=b.id, kind="乱写")
    with pytest.raises(ValueError):
        await store.create_edge(db_session, from_card_id=a.id, to_card_id=99999, kind="caused_by")

    edge = await store.create_edge(db_session, from_card_id=b.id, to_card_id=a.id,
                                   kind="caused_by", source_session_id="s1")
    await store.create_edge(db_session, from_card_id=b.id, to_card_id=e.id, kind="led_to")
    await db_session.commit()
    assert edge.id is not None

    # caused_by 约定：from=结果、to=原因；查起因 / 反查结果（数据库文档 §5）
    causes = await store.list_edges(db_session, card_id=b.id, kind="caused_by", direction="from")
    assert [(x.from_card_id, x.to_card_id) for x in causes] == [(b.id, a.id)]
    results = await store.list_edges(db_session, card_id=a.id, kind="caused_by", direction="to")
    assert [x.from_card_id for x in results] == [b.id]
    assert len(await store.list_edges(db_session, card_id=b.id)) == 2
    with pytest.raises(ValueError):
        await store.list_edges(db_session, direction="side")


# ================================
# 明文（messages）CRUD
# ================================

@pytest.mark.asyncio
async def test_messages_append_and_list(db_session):
    await store.append_message(db_session, session_id="s1", role="user", content="你好")
    await store.append_message(db_session, session_id="s1", role="assistant", content="你好呀")
    await store.append_message(db_session, session_id="s2", role="user", content="别的会话")
    await db_session.commit()

    rows = await store.list_messages(db_session, session_id="s1")
    assert [(m.role, m.content) for m in rows] == [("user", "你好"), ("assistant", "你好呀")]
    assert len(await store.list_messages(db_session, session_id="s1", after_id=rows[0].id)) == 1
    assert len(await store.list_messages(db_session, session_id="s1", limit=1)) == 1
    with pytest.raises(ValueError):
        await store.append_message(db_session, session_id="s1", role="system", content="x")


@pytest.mark.asyncio
async def test_search_messages_or_desc_and_limit(db_session):
    await store.append_message(db_session, session_id="s1", role="user", content="我上个月在学吉他")
    await store.append_message(db_session, session_id="s2", role="user", content="下周要去露营")
    await store.append_message(db_session, session_id="s1", role="assistant", content="练吉他要注意护手")
    await db_session.commit()

    rows = await store.search_messages(db_session, keywords=["吉他"])          # 跨会话 + id 降序
    assert [m.content for m in rows] == ["练吉他要注意护手", "我上个月在学吉他"]
    assert [m.content for m in
            await store.search_messages(db_session, keywords=["吉他"], limit=1)] == ["练吉他要注意护手"]

    rows = await store.search_messages(db_session, keywords=["露营", "护手"])  # OR：任一词命中
    assert [m.content for m in rows] == ["练吉他要注意护手", "下周要去露营"]


@pytest.mark.asyncio
async def test_search_messages_escapes_and_empty_keywords(db_session):
    await store.append_message(db_session, session_id="s1", role="user", content="折扣 50% 的券")
    await store.append_message(db_session, session_id="s1", role="user", content="折扣 5099 的券")
    await store.append_message(db_session, session_id="s1", role="user", content="用 GitHub 托管代码")
    await db_session.commit()

    assert await store.search_messages(db_session, keywords=[]) == []          # 空：不发 SQL
    assert await store.search_messages(db_session, keywords=["", " ", "  "]) == []

    rows = await store.search_messages(db_session, keywords=["50%"])
    assert [m.content for m in rows] == ["折扣 50% 的券"]                      # % 按字面量（非通配符）
    rows = await store.search_messages(db_session, keywords=["github"])
    assert [m.content for m in rows] == ["用 GitHub 托管代码"]                 # 大小写不敏感


# ================================
# 写序列编排：DB → 快表 → Qdrant
# ================================

@pytest.mark.asyncio
async def test_commit_and_sync_order_pool_before_qdrant(db_session, vector_stack):
    events: list[str] = []
    store.register_pool_hook(lambda action, payload: events.append(f"pool:{action}"))
    orig_upsert = vector_stack.upsert_points

    def spy(points):
        events.append("qdrant:upsert")
        orig_upsert(points)

    vector_stack.upsert_points = spy

    a = await _card(db_session, "甲")
    b = await _card(db_session, "乙")
    await store.commit_and_sync(db_session, upsert_cards=[a, b])

    assert events == ["pool:upsert", "qdrant:upsert"]          # 快表先于 Qdrant（D5）
    assert vector_stack.card_ids() == {a.id, b.id}
    assert vector_stack.points[a.id]["payload"]["type"] == "card"
    # DB 已提交（同会话复查可见）
    assert (await db_session.execute(select(func.count()).select_from(Card))).scalar_one() == 2


@pytest.mark.asyncio
async def test_commit_and_sync_nonactive_routed_to_delete(db_session, vector_stack):
    a = await _card(db_session, "先建再撤销")
    await store.revoke_card(db_session, a.id)
    await store.commit_and_sync(db_session, upsert_cards=[a])   # 非 active → 自动改道删除

    assert vector_stack.card_ids() == set()
    assert store.pending_vector_ops() == 0


@pytest.mark.asyncio
async def test_qdrant_down_commit_succeeds_and_queues(db_session, vector_stack):
    vector_stack.fail = True
    a = await _card(db_session, "带伤提交")
    await store.commit_and_sync(db_session, upsert_cards=[a])

    # 验收铁律：Qdrant 挂掉不影响 DB 提交
    assert (await db_session.execute(
        select(Card).where(Card.id == a.id))).scalar_one().content == "带伤提交"
    assert store.pending_vector_ops() == 1


@pytest.mark.asyncio
async def test_flush_retry_queue_drains_when_recovered(db_session, vector_stack):
    vector_stack.fail = True
    a = await _card(db_session, "等恢复")
    await store.commit_and_sync(db_session, upsert_cards=[a])
    assert store.pending_vector_ops() == 1

    assert await store.flush_retry_queue() == {"flushed": 0, "pending": 1}   # 未恢复：不抛、保留

    vector_stack.fail = False
    assert await store.flush_retry_queue() == {"flushed": 1, "pending": 0}   # 恢复：冲掉
    assert vector_stack.card_ids() == {a.id}


# ================================
# 启动对账（D17 不变量：集合 = DB active 集合）
# ================================

@pytest.mark.asyncio
async def test_reconcile_repairs_drift(db_session, vector_stack):
    a = await _card(db_session, "在库卡")
    b = await _card(db_session, "缺写卡")
    await db_session.commit()
    vector_stack.points[9999] = {"vector": [0, 0, 0], "payload": {"type": "card"}}   # 脏残留

    report = await store.reconcile_qdrant(db_session)
    assert report["upserted"] == 2 and report["deleted"] == 1
    assert vector_stack.card_ids() == {a.id, b.id}

    report = await store.reconcile_qdrant(db_session)           # 幂等：再对一次零漂移
    assert (report["upserted"], report["deleted"]) == (0, 0)


@pytest.mark.asyncio
async def test_supersede_flow_vector_follows_states(db_session, vector_stack):
    v1 = await _card(db_session, "第一版：住上海")
    await store.commit_and_sync(db_session, upsert_cards=[v1])
    assert vector_stack.card_ids() == {v1.id}

    v2 = await _card(db_session, "第二版：住北京")
    await store.supersede_card(db_session, new_card_id=v2.id, old_card_id=v1.id)
    await store.commit_and_sync(db_session, upsert_cards=[v2], delete_card_ids=[v1.id])

    assert vector_stack.card_ids() == {v2.id}                   # 旧版向量即删（D17 防占名额）
    report = await store.reconcile_qdrant(db_session)           # 与 DB active 集合零漂移
    assert (report["upserted"], report["deleted"]) == (0, 0)


# ================================
# 一致性检查与启动钩子
# ================================

@pytest.mark.asyncio
async def test_check_and_rebuild_status_detects_drift(db_session):
    torn_old = await _card(db_session, "旧地址")
    torn_new = await _card(db_session, "新地址")
    db_session.add(Edge(from_card_id=torn_new.id, to_card_id=torn_old.id, kind="supersedes"))
    await db_session.commit()

    assert await store.check_and_rebuild_status(db_session) == {"drift": 1, "fixed": 1}
    assert torn_old.status == "superseded"
    assert await store.check_and_rebuild_status(db_session) == {"drift": 0, "fixed": 0}


@pytest.mark.asyncio
async def test_startup_check_degrades_when_qdrant_down(db_session, vector_stack):
    vector_stack.fail = True
    await _card(db_session, "正常卡")
    await db_session.commit()

    report = await store.startup_check(db_session)              # 不抛出（启动永不阻塞）
    assert report["drift"] == 0
    assert "error" in report["vector"]


@pytest.mark.asyncio
async def test_startup_check_reconciles_when_healthy(db_session, vector_stack):
    a = await _card(db_session, "启动补写")
    await db_session.commit()

    report = await store.startup_check(db_session)
    assert report["vector"]["upserted"] == 1
    assert vector_stack.card_ids() == {a.id}


# ================================
# 向量化（provider 双轨 + MRL 截断归一）
# ================================

@pytest.mark.asyncio
async def test_embed_api_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "api")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", None)
    with pytest.raises(RuntimeError):
        await store.embed_texts(["x"])


@pytest.mark.asyncio
async def test_embed_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "magic")
    with pytest.raises(ValueError):
        await store.embed_texts(["x"])


def test_finalize_vector_truncates_and_normalizes(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 2)
    assert store._finalize_vector([3.0, 4.0, 9.0, 9.0]) == pytest.approx([0.6, 0.8])
    with pytest.raises(ValueError):
        store._finalize_vector([1.0])                           # 维度不足 = 配置不匹配
