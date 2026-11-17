"""记忆管理端点单测（E11）：列表/详情/编辑/撤销/恢复/回滚/硬删/剪断/统计/淘汰/日志。

策略：httpx ASGITransport（与测试同一事件循环）+ get_session 依赖覆盖为测试库会话；
Qdrant 面关闭；淘汰/编辑日志写 tmp_path。
"""
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints import memory as memory_api
from app.core.config import settings
from app.main import app
from app.services.long_term_memory import store


@pytest.fixture
def api(db_session, monkeypatch, tmp_path):
    """测试用 ASGI 客户端：依赖覆盖 + 外部面隔离。"""
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)
    monkeypatch.setattr(settings, "EVICT_LOG_PATH", str(tmp_path / "evict_log.jsonl"))
    monkeypatch.setattr(settings, "EDIT_LOG_PATH", str(tmp_path / "edit_log.jsonl"))
    store.register_pool_hook(None)

    async def _override_session():
        yield db_session

    app.dependency_overrides[memory_api.get_session] = _override_session
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.clear()


async def _mk(session, content="卡", kind="entity", subtype=None, **kw):
    return await store.create_card(session, kind=kind, content=content,
                                   subtype=subtype, **kw)


# ================================
# 列表 / 统计 / 参数校验
# ================================

@pytest.mark.asyncio
async def test_list_filters_and_stats(api, db_session):
    old = await _mk(db_session, "旧版本卡")
    new = await _mk(db_session, "新版本卡")
    await store.supersede_card(db_session, new_card_id=new.id, old_card_id=old.id)
    await db_session.commit()

    body = (await api.get("/api/v1/memory", params={"status": "active"})).json()
    assert [c["id"] for c in body["items"]] == [new.id]
    assert body["count"] == 1

    body_all = (await api.get("/api/v1/memory")).json()          # 不传 status = 全量
    assert body_all["count"] == 2
    assert body_all["items"][0]["id"] == new.id                  # desc：最新在前

    stats = (await api.get("/api/v1/memory/stats")).json()
    assert stats["total"] == 2
    assert stats["by_status"] == {"active": 1, "superseded": 1}
    assert "pool" in stats


@pytest.mark.asyncio
async def test_list_invalid_status_400(api):
    resp = await api.get("/api/v1/memory", params={"status": "bogus"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_detail_not_found_404(api):
    resp = await api.get("/api/v1/memory/999")
    assert resp.status_code == 404


# ================================
# 详情（版本链 / 取代者 / 边）
# ================================

@pytest.mark.asyncio
async def test_detail_versions_and_superseded_by(api, db_session):
    old = await _mk(db_session, "旧版本")
    new = await _mk(db_session, "新版本")
    await store.supersede_card(db_session, new_card_id=new.id, old_card_id=old.id)
    await db_session.commit()

    r_old = (await api.get(f"/api/v1/memory/{old.id}")).json()
    assert r_old["status"] == "superseded"
    assert r_old["superseded_by"] == new.id

    r_new = (await api.get(f"/api/v1/memory/{new.id}")).json()
    assert r_new["superseded_by"] is None
    assert [v["id"] for v in r_new["versions"]] == [old.id]      # 版本历史（近→远）
    assert any(e["kind"] == "supersedes" for e in r_new["edges"])


# ================================
# 编辑（PATCH → 新版本 + supersedes）
# ================================

@pytest.mark.asyncio
async def test_patch_creates_new_version(api, db_session):
    old = await _mk(db_session, "喜欢红茶")
    await db_session.commit()

    resp = await api.patch(f"/api/v1/memory/{old.id}", json={"content": "改喝绿茶了"})
    assert resp.status_code == 200
    new_id = resp.json()["new_card_id"]

    assert (await store.get_card(db_session, old.id)).status == "superseded"
    new_card = await store.get_card(db_session, new_id)
    assert new_card.status == "active"
    assert new_card.content == "改喝绿茶了"
    assert (await store.list_edges(db_session, kind="supersedes"))[0].to_card_id == old.id


@pytest.mark.asyncio
async def test_patch_preserves_node_identity_and_provenance(api, db_session):
    """修订继承身份字段：节点卡改内容后仍是节点（canonical_name），来源原文不丢。"""
    old = await _mk(db_session, "Caroline 在特殊教育学校工作", subtype="person",
                    canonical_name="Caroline", source_message_ids=[11, 12])
    await db_session.commit()

    resp = await api.patch(f"/api/v1/memory/{old.id}",
                           json={"content": "Caroline 在一所特殊教育学校当老师"})
    assert resp.status_code == 200
    new_card = await store.get_card(db_session, resp.json()["new_card_id"])
    assert new_card.canonical_name == "Caroline"
    assert new_card.source_message_ids == [11, 12]


@pytest.mark.asyncio
async def test_patch_rejects_non_active(api, db_session):
    old = await _mk(db_session, "旧")
    new = await _mk(db_session, "新")
    await store.supersede_card(db_session, new_card_id=new.id, old_card_id=old.id)
    await db_session.commit()
    resp = await api.patch(f"/api/v1/memory/{old.id}", json={"content": "改"})
    assert resp.status_code == 400
    assert "active" in resp.json()["reply"]


# ================================
# 撤销 / 恢复 / 回滚 / 硬删
# ================================

@pytest.mark.asyncio
async def test_delete_restore_roundtrip(api, db_session):
    card = await _mk(db_session, "待删卡")
    await db_session.commit()

    resp = await api.delete(f"/api/v1/memory/{card.id}")
    assert resp.status_code == 200 and resp.json()["action"] == "delete"
    assert (await store.get_card(db_session, card.id)).status == "revoked"

    resp2 = await api.post(f"/api/v1/memory/{card.id}/restore")
    assert resp2.status_code == 200
    assert (await store.get_card(db_session, card.id)).status == "active"


@pytest.mark.asyncio
async def test_restore_rejected_for_superseded(api, db_session):
    old = await _mk(db_session, "旧")
    new = await _mk(db_session, "新")
    await store.supersede_card(db_session, new_card_id=new.id, old_card_id=old.id)
    await db_session.commit()
    resp = await api.post(f"/api/v1/memory/{old.id}/restore")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rollback_endpoint(api, db_session):
    old = await _mk(db_session, "旧版")
    new = await _mk(db_session, "新版")
    await store.supersede_card(db_session, new_card_id=new.id, old_card_id=old.id)
    await db_session.commit()

    resp = await api.post(f"/api/v1/memory/{old.id}/rollback")
    assert resp.status_code == 200
    body = resp.json()
    assert body["affected_card_ids"] == [new.id]
    assert len(body["cut_edge_ids"]) == 1
    assert (await store.get_card(db_session, old.id)).status == "active"
    assert (await store.get_card(db_session, new.id)).status == "revoked"


@pytest.mark.asyncio
async def test_purge_endpoint(api, db_session):
    card = await _mk(db_session, "将被遗忘")
    await db_session.commit()

    resp = await api.delete(f"/api/v1/memory/{card.id}/purge")
    assert resp.status_code == 200 and resp.json()["action"] == "purge"
    assert (await store.get_card(db_session, card.id)) is None
    assert (await api.get(f"/api/v1/memory/{card.id}")).status_code == 404


# ================================
# 剪断边（因果边可剪 / 版本边拒绝）
# ================================

@pytest.mark.asyncio
async def test_cut_edge_endpoint_and_guard(api, db_session):
    a = await _mk(db_session, "A")
    b = await _mk(db_session, "B")
    causal = await store.create_edge(db_session, from_card_id=b.id, to_card_id=a.id,
                                     kind="caused_by")
    version = await store.supersede_card(db_session, new_card_id=b.id, old_card_id=a.id)
    await db_session.commit()

    resp = await api.delete(f"/api/v1/memory/edges/{causal.id}")
    assert resp.status_code == 200 and resp.json()["action"] == "cut_edge"
    assert (await store.get_edge(db_session, causal.id)).cut_at is not None

    resp2 = await api.delete(f"/api/v1/memory/edges/{version.id}")
    assert resp2.status_code == 400                               # 版本边拒绝
    assert "rollback" in resp2.json()["reply"]


# ================================
# 淘汰巡检 / 淘汰日志
# ================================

@pytest.mark.asyncio
async def test_evict_endpoint_and_log(api, db_session):
    goal = await _mk(db_session, "过期目标", subtype="goal",
                     event_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
    await db_session.commit()

    resp = await api.post("/api/v1/memory/evict")
    assert resp.status_code == 200
    body = resp.json()
    assert body["evicted"] >= 1
    assert body["details"][0]["rule"] == "goal_expired"
    assert (await store.get_card(db_session, goal.id)).status == "evicted"

    log = (await api.get("/api/v1/memory/evict-log")).json()
    assert log["items"][0]["card_id"] == goal.id
    assert log["items"][0]["rule"] == "goal_expired"
