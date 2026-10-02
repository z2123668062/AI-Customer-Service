"""store 编辑层原语单测（E11）：淘汰 / 恢复 / 剪断 / 回滚 / 硬删 + 剪断轨同步。

策略：与 test_store_crud 同款——Qdrant 面关闭（向量同步另有专门用例）；
快表钩子用列表捕获（验证 edge_cut / delete 的同步轨载荷）。
"""
from datetime import datetime, timezone

import pytest

from app.services.long_term_memory import store


@pytest.fixture
def clean_sync(monkeypatch):
    """隔离：关闭 Qdrant 面 + 干净重试队列 + 捕获钩子调用。"""
    monkeypatch.setattr(store.settings, "QDRANT_ENABLED", False)
    calls: list = []
    store._retry_queue.clear()
    store.register_pool_hook(lambda action, payload: calls.append((action, payload)))
    yield calls
    store.register_pool_hook(None)
    store._retry_queue.clear()


async def _mk(session, content="卡", kind="entity", subtype=None, **kw):
    return await store.create_card(session, kind=kind, content=content, subtype=subtype, **kw)


# ================================
# 淘汰（evict_cards）
# ================================

@pytest.mark.asyncio
async def test_evict_cards_batch_and_idempotent(db_session):
    a, b = await _mk(db_session, "A"), await _mk(db_session, "B")
    await store.revoke_card(db_session, b.id)          # 非 active 不参与
    changed = await store.evict_cards(db_session, [a.id, b.id, 999])   # 999 不存在静默跳过
    assert changed == [a.id]
    assert (await store.get_card(db_session, a.id)).status == "evicted"
    assert (await store.get_card(db_session, b.id)).status == "revoked"
    assert await store.evict_cards(db_session, [a.id]) == []           # 幂等


@pytest.mark.asyncio
async def test_evict_sync_track(clean_sync, db_session):
    a = await _mk(db_session, "A")
    ids = await store.evict_cards(db_session, [a.id])
    await store.commit_and_sync(db_session, delete_card_ids=ids)
    assert ("delete", [a.id]) in clean_sync            # 非 active → 快表删除轨


# ================================
# 剪断（cut_edge + list_edges 剪断过滤）
# ================================

@pytest.mark.asyncio
async def test_cut_edge_causal_and_idempotent(db_session):
    a, b = await _mk(db_session, "A"), await _mk(db_session, "B")
    edge = await store.create_edge(db_session, from_card_id=b.id, to_card_id=a.id,
                                   kind="caused_by")
    assert await store.cut_edge(db_session, edge.id) is True
    assert await store.cut_edge(db_session, edge.id) is False          # 幂等
    assert (await store.get_edge(db_session, edge.id)).cut_at is not None
    assert await store.list_edges(db_session, card_id=a.id) == []      # 默认排除剪断边
    full = await store.list_edges(db_session, card_id=a.id, include_cut=True)
    assert [e.id for e in full] == [edge.id]


@pytest.mark.asyncio
async def test_cut_edge_rejects_supersedes(db_session):
    a, b = await _mk(db_session, "A"), await _mk(db_session, "B")
    edge = await store.supersede_card(db_session, new_card_id=b.id, old_card_id=a.id)
    with pytest.raises(ValueError, match="版本边"):
        await store.cut_edge(db_session, edge.id)


@pytest.mark.asyncio
async def test_cut_edge_sync_track(clean_sync, db_session):
    a, b = await _mk(db_session, "A"), await _mk(db_session, "B")
    edge = await store.create_edge(db_session, from_card_id=b.id, to_card_id=a.id,
                                   kind="caused_by")
    await store.cut_edge(db_session, edge.id)
    await store.commit_and_sync(db_session, cut_edges=[edge])
    assert ("edge_cut", [(b.id, a.id, "caused_by")]) in clean_sync


@pytest.mark.asyncio
async def test_rebuild_ignores_cut_edges(db_session):
    a, b = await _mk(db_session, "A"), await _mk(db_session, "B")
    edge = await store.supersede_card(db_session, new_card_id=b.id, old_card_id=a.id)
    assert (await store.get_card(db_session, a.id)).status == "superseded"
    edge.cut_at = datetime.now(timezone.utc)           # 直落列（绕过守卫，模拟剪断）
    await db_session.flush()
    await store.rebuild_status(db_session)
    assert (await store.get_card(db_session, a.id)).status == "active"  # 剪断边不参与推导


# ================================
# 恢复（restore_card）
# ================================

@pytest.mark.asyncio
async def test_restore_card_paths(db_session):
    a = await _mk(db_session, "A")
    await store.evict_cards(db_session, [a.id])
    assert await store.restore_card(db_session, a.id) is True          # evicted → active
    assert (await store.get_card(db_session, a.id)).status == "active"
    assert await store.restore_card(db_session, a.id) is False         # 已 active

    await store.revoke_card(db_session, a.id)
    assert await store.restore_card(db_session, a.id) is True          # revoked → active

    b = await _mk(db_session, "B")
    await store.supersede_card(db_session, new_card_id=b.id, old_card_id=a.id)
    with pytest.raises(ValueError, match="rollback"):                  # superseded → 引导回滚
        await store.restore_card(db_session, a.id)
    await store.revoke_card(db_session, a.id)                          # 撤销但入边仍在
    with pytest.raises(ValueError, match="rollback"):                  # 有未剪断入边 → 拒绝
        await store.restore_card(db_session, a.id)


# ================================
# 回滚（rollback_card）
# ================================

@pytest.mark.asyncio
async def test_rollback_card_chain(db_session):
    a, b, c = await _mk(db_session, "A"), await _mk(db_session, "B"), await _mk(db_session, "C")
    e1 = await store.supersede_card(db_session, new_card_id=b.id, old_card_id=a.id)
    e2 = await store.supersede_card(db_session, new_card_id=c.id, old_card_id=b.id)

    result = await store.rollback_card(db_session, a.id)
    assert result["restored"].id == a.id
    assert result["restored"].status == "active"
    assert sorted(x.id for x in result["revoked"]) == [b.id, c.id]
    assert sorted(x.id for x in result["cut_edges"]) == [e1.id, e2.id]
    assert all(x.cut_at is not None for x in result["cut_edges"])
    assert (await store.get_card(db_session, b.id)).status == "revoked"
    assert (await store.get_card(db_session, c.id)).status == "revoked"
    assert await store.list_edges(db_session, kind="supersedes") == []  # 边全剪断（默认排除）

    # 回滚后的 A 经重建不动摇（无未剪断入边 → active 是真理）
    await store.rebuild_status(db_session)
    assert (await store.get_card(db_session, a.id)).status == "active"


@pytest.mark.asyncio
async def test_rollback_card_guard(db_session):
    a = await _mk(db_session, "A")
    with pytest.raises(ValueError, match="superseded"):
        await store.rollback_card(db_session, a.id)


# ================================
# 硬删（purge_card）
# ================================

@pytest.mark.asyncio
async def test_purge_card_physical_and_restore_old(db_session):
    a, b = await _mk(db_session, "A"), await _mk(db_session, "B")
    await store.supersede_card(db_session, new_card_id=b.id, old_card_id=a.id)

    result = await store.purge_card(db_session, b.id)
    assert result["card"].id == b.id
    assert result["edges_deleted"] == 1
    assert [c.id for c in result["restored"]] == [a.id]                # 旧卡回荡 active
    assert (await store.get_card(db_session, b.id)) is None            # 物理删除
    assert (await store.get_card(db_session, a.id)).status == "active"
    assert await store.list_edges(db_session, include_cut=True) == []  # 边连删


@pytest.mark.asyncio
async def test_purge_card_deletes_incoming_edges(db_session):
    a, b = await _mk(db_session, "A"), await _mk(db_session, "B")
    await store.create_edge(db_session, from_card_id=b.id, to_card_id=a.id, kind="led_to")
    result = await store.purge_card(db_session, a.id)
    assert result["edges_deleted"] == 1
    assert await store.list_edges(db_session, include_cut=True) == []
    assert (await store.get_card(db_session, b.id)) is not None        # 对端卡不受影响
