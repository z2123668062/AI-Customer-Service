"""淘汰路径单测（E11）：分类型规则 / 时钟注入 / 消融跳过 / 日志落盘与回读。

策略：确定性——now 注入固定时钟；Qdrant 面关闭（向量同步另有专门用例）；
淘汰日志写 tmp_path（隔离真实 data/ 目录）。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.core.config import settings
from app.models.domain import Card
from app.services.long_term_memory import evictor, store

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """隔离：Qdrant 关闭 + 日志指向 tmp + 清池钩子。"""
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)
    monkeypatch.setattr(settings, "EVICT_LOG_PATH", str(tmp_path / "evict_log.jsonl"))
    store.register_pool_hook(None)
    yield


async def _mk(session, content, kind="entity", subtype=None, **kw):
    return await store.create_card(session, kind=kind, content=content,
                                   subtype=subtype, **kw)


async def _age(session, card, *, created_days=None, accessed_days=None):
    """拨老时间字段（纯 SQL，避免 async 下属性过期触发的隐式 IO）。"""
    values: dict = {}
    if created_days is not None:
        values["created_at"] = NOW - timedelta(days=created_days)
    if accessed_days is not None:
        values["last_accessed_at"] = NOW - timedelta(days=accessed_days)
    if values:
        await session.execute(
            update(Card).where(Card.id == card.id).values(**values)
            .execution_options(synchronize_session="fetch")
        )
        await session.flush()


# ================================
# 分类型规则
# ================================

@pytest.mark.asyncio
async def test_goal_expired_evicted(sandbox, db_session):
    old = await _mk(db_session, "月底学潜水", subtype="goal",
                    event_time=NOW - timedelta(days=40))          # 40 > 宽限 30 天
    fresh = await _mk(db_session, "下月学吉他", subtype="goal",
                      event_time=NOW + timedelta(days=10))
    report = await evictor.sweep(db_session, now=NOW)
    assert report["evicted"] == 1
    assert (await store.get_card(db_session, old.id)).status == "evicted"
    assert (await store.get_card(db_session, fresh.id)).status == "active"
    assert report["details"][0]["rule"] == "goal_expired"


@pytest.mark.asyncio
async def test_goal_within_grace_kept(sandbox, db_session):
    await _mk(db_session, "本周交报告", subtype="goal",
              event_time=NOW - timedelta(days=3))
    report = await evictor.sweep(db_session, now=NOW)
    assert report["evicted"] == 0


@pytest.mark.asyncio
async def test_goal_without_event_time_kept(sandbox, db_session):
    await _mk(db_session, "某天学做饭", subtype="goal")            # 无时间 → 不判
    report = await evictor.sweep(db_session, now=NOW)
    assert report["evicted"] == 0


@pytest.mark.asyncio
async def test_preference_decay_and_explicit_relaxed(sandbox, db_session):
    p_old = await _mk(db_session, "喜欢美式", subtype="preference")
    e_old = await _mk(db_session, "请记住我喝燕麦拿铁", subtype="preference",
                      source="explicit")
    await _age(db_session, p_old, created_days=200)   # passive 半衰期 90 → 180 天下架线
    await _age(db_session, e_old, created_days=200)   # explicit 半衰期 180 → 360 天下架线
    report = await evictor.sweep(db_session, now=NOW)
    assert report["evicted"] == 1
    assert (await store.get_card(db_session, p_old.id)).status == "evicted"
    assert (await store.get_card(db_session, e_old.id)).status == "active"
    assert report["details"][0]["rule"] == "preference_decay"


@pytest.mark.asyncio
async def test_preference_accessed_renews(sandbox, db_session):
    p = await _mk(db_session, "喜欢喝茶", subtype="preference")
    await _age(db_session, p, created_days=400, accessed_days=5)     # 老卡但最近被读
    report = await evictor.sweep(db_session, now=NOW)
    assert report["evicted"] == 0                                    # last_accessed 续命


@pytest.mark.asyncio
async def test_exempt_and_event_not_decayed(sandbox, db_session):
    ident = await _mk(db_session, "用户是程序员", subtype="identity")
    rel = await _mk(db_session, "用户和王五是同事", subtype="relation")
    ev = await _mk(db_session, "搬到了杭州", kind="event")
    for card in (ident, rel, ev):
        await _age(db_session, card, created_days=500)
    report = await evictor.sweep(db_session, now=NOW)
    assert report["evicted"] == 0


@pytest.mark.asyncio
async def test_sweep_only_scans_active(sandbox, db_session):
    a = await _mk(db_session, "过期目标A", subtype="goal", event_time=NOW - timedelta(days=99))
    b = await _mk(db_session, "被取代的老目标", subtype="goal", event_time=NOW - timedelta(days=99))
    c = await _mk(db_session, "撤回的目标", subtype="goal", event_time=NOW - timedelta(days=99))
    new = await _mk(db_session, "新目标", subtype="goal")
    await store.supersede_card(db_session, new_card_id=new.id, old_card_id=b.id)
    await store.revoke_card(db_session, c.id)

    report = await evictor.sweep(db_session, now=NOW)
    assert report["scanned"] == 2                                    # 只扫 active（a + new）
    assert report["evicted"] == 1
    assert (await store.get_card(db_session, a.id)).status == "evicted"
    assert (await store.get_card(db_session, b.id)).status == "superseded"  # 不动
    assert (await store.get_card(db_session, c.id)).status == "revoked"     # 不动


# ================================
# 日志
# ================================

@pytest.mark.asyncio
async def test_sweep_logs_and_reads_back(sandbox, db_session):
    g1 = await _mk(db_session, "过期目标一", subtype="goal", event_time=NOW - timedelta(days=40))
    g2 = await _mk(db_session, "过期目标二", subtype="goal", event_time=NOW - timedelta(days=50))
    await evictor.sweep(db_session, now=NOW)

    records = evictor.read_evict_log(limit=10)
    assert len(records) == 2
    assert {r["card_id"] for r in records} == {g1.id, g2.id}
    assert all(r["rule"] == "goal_expired" for r in records)
    assert all(r["ts"] == NOW.isoformat() for r in records)          # 时钟注入进日志
    assert evictor.read_evict_log(limit=1)[0]["card_id"] == g2.id    # 倒序：最新在前


@pytest.mark.asyncio
async def test_log_fail_open(sandbox, monkeypatch, db_session):
    monkeypatch.setattr(evictor, "_log_path", lambda: "Z:/绝对不存在的盘/x.jsonl")
    await _mk(db_session, "过期目标", subtype="goal", event_time=NOW - timedelta(days=40))
    report = await evictor.sweep(db_session, now=NOW)                # 日志写失败不回退淘汰
    assert report["evicted"] == 1
