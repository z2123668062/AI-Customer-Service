"""快表单测（E6）：五结构 / 有界工作集 / 两段式替换（SLRU + 换出记录簿）/ 缺页通知 / 写轨同步。

策略：替换与缺页用假卡直测（纯内存、零 DB，同时实证"更新路径零 IO"）；
构建与写轨联调用 db_session + store（验证真实链路：commit_and_sync → 钩子 → 快表）。
"""
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.long_term_memory import pool, store


def _fake(card_id, content=None, kind="entity", entities=("甲",), event_time=None):
    """假卡（与 ORM Card 鸭子类型同形），供替换/缺页类测试零 DB 直测。"""
    return SimpleNamespace(
        id=card_id, kind=kind, subtype=None,
        content=content or f"卡{card_id}", entities=list(entities),
        event_time=event_time, source="passive", created_at=None, status="active",
    )


@pytest.fixture(autouse=True)
def clean_pool():
    pool.clear()
    pool.register_fault_hook(None)
    store.register_pool_hook(None)
    yield
    pool.clear()
    pool.register_fault_hook(None)
    store.register_pool_hook(None)


@pytest.fixture
def small_pool(monkeypatch):
    """容量 4（保护层配额 3）、记录簿 2——替换行为几个动作内可见。"""
    monkeypatch.setattr(pool, "POOL_CAPACITY", 4)
    monkeypatch.setattr(pool, "GHOST_SIZE", 2)
    yield


# ================================
# 写轨同步（upsert / delete / edge_upsert）
# ================================

def test_upsert_and_delete_roundtrip():
    pool.on_store_write("upsert", [_fake(1), _fake(2)])
    hits, missing = pool.get_cards([1, 2])
    assert [c.id for c in hits] == [1, 2] and missing == []

    pool.on_store_write("delete", [1])
    hits, missing = pool.get_cards([1])
    assert hits == [] and missing == [1]
    assert pool.stats()["cards"] == 1


def test_upsert_skips_non_active_defensively():
    card = _fake(1)
    card.status = "revoked"
    pool.on_store_write("upsert", [card])
    assert pool.stats()["cards"] == 0


def test_upsert_existing_refreshes_without_duplicate():
    pool.on_store_write("upsert", [_fake(1, entities=("甲",))])
    pool.on_store_write("upsert", [_fake(1, content="新文本", entities=("乙",))])
    hits, _ = pool.get_cards([1])
    assert hits[0].content == "新文本"
    assert pool.stats()["cards"] == 1
    assert pool.by_entities(["甲"])[0] == []              # 旧实体索引已摘除
    assert pool.by_entities(["乙"])[0] == [1]


def test_delete_is_not_eviction_no_ghost():
    pool.on_store_write("upsert", [_fake(1)])
    pool.on_store_write("delete", [1])
    assert pool.stats()["ghost"] == 0                     # 语义移除 ≠ 换出（D18：不入记录簿）


def test_edge_upsert_requires_both_on_board():
    pool.on_store_write("upsert", [_fake(1), _fake(2)])
    pool.on_store_write("edge_upsert", [(1, 2, "caused_by"), (1, 99, "led_to")])
    assert pool.neighbors(1) == [(2, "caused_by")]        # 99 不在册：整条不装
    assert pool.neighbors(2) == [(1, "caused_by")]

    pool.on_store_write("upsert", [_fake(99)])            # 先补卡、再补线（同一通道）
    pool.on_store_write("edge_upsert", [(1, 99, "led_to")])
    assert (99, "led_to") in pool.neighbors(1)


def test_delete_cleans_edges_and_indexes_both_sides():
    pool.on_store_write("upsert", [_fake(1, entities=("张三",)), _fake(2, entities=("张三",))])
    pool.on_store_write("edge_upsert", [(1, 2, "caused_by")])

    pool.on_store_write("delete", [2])
    assert pool.neighbors(1) == []                        # 邻居侧同步摘除
    assert pool.by_entities(["张三"])[0] == [1]           # 实体索引只留活着的那张
    assert pool.stats()["edges"] == 0


# ================================
# 读接口：三入口 + 缺页通知
# ================================

def test_read_miss_notifies_fault_hook():
    events = []
    pool.register_fault_hook(lambda kind, keys: events.append((kind, keys)))
    hits, missing = pool.get_cards([7, 8])
    assert hits == [] and missing == [7, 8]
    assert events == [("cards", [7, 8])]
    assert pool.stats()["faults_cards"] == 1


def test_get_card_miss_reports():
    events = []
    pool.register_fault_hook(lambda kind, keys: events.append((kind, keys)))
    assert pool.get_card(5) is None
    assert events == [("cards", [5])]


def test_fault_hook_exception_never_breaks_read():
    def boom(kind, keys):
        raise RuntimeError("boom")

    pool.register_fault_hook(boom)
    hits, missing = pool.get_cards([999])                 # 不抛出（钩子失败只告警）
    assert hits == [] and missing == [999]


def test_entity_entry_and_missing_report():
    pool.on_store_write("upsert", [_fake(1, entities=("张三", "上海")),
                                   _fake(2, entities=("张三",))])
    events = []
    pool.register_fault_hook(lambda kind, keys: events.append((kind, keys)))

    ids, missing = pool.by_entities(["张三", "李四", "张三"])
    assert ids == [1, 2] and missing == ["李四"]          # 去重 + 缺失上报
    assert events == [("entities", ["李四"])]

    pool.on_store_write("upsert", [_fake(3, entities=("Alice",))])
    assert pool.by_entities([" alice "])[0] == [3]        # 归一化命中


def test_known_entities_sorted_and_normalized():
    pool.on_store_write("upsert", [
        _fake(1, entities=("张三", "Alice")),
        _fake(2, entities=(" bob ", "张三")),
    ])
    assert pool.known_entities() == ["alice", "bob", "张三"]   # 归一化 + 排序


def test_known_entities_empty_pool():
    assert pool.known_entities() == []


def test_time_range_half_open_and_none_excluded():
    t1 = datetime(2026, 1, 1)
    t2 = datetime(2026, 2, 1)
    t3 = datetime(2026, 3, 1)
    pool.on_store_write("upsert", [
        _fake(1, event_time=t1), _fake(2, event_time=t2),
        _fake(3, event_time=t3), _fake(4, event_time=None),
    ])
    assert pool.by_time_range(t1, t3) == [1, 2]           # [start, end) 半开
    assert pool.by_time_range(datetime(2025, 1, 1), datetime(2027, 1, 1)) == [1, 2, 3]
    assert pool.by_time_range(t2, t2) == []


def test_time_range_normalizes_naive_and_aware_mixed():
    """时区归一（D24）：naive/aware 混存不再炸——快照与入参统一 aware UTC 口径。"""
    pool.on_store_write("upsert", [
        _fake(1, event_time=datetime(2026, 5, 1, 8, 0)),                     # naive（SQLite 读回口径）
        _fake(2, event_time=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)),  # aware（LLM 带时区输出）
    ])
    hits, _ = pool.get_cards([1, 2])
    assert all(c.event_time.tzinfo is not None for c in hits)               # 快照一律归一 aware

    assert pool.by_time_range(datetime(2026, 5, 1), datetime(2026, 5, 2)) == [1, 2]
    # 入参防御：aware 窗口与归一后的轴对齐
    assert pool.by_time_range(datetime(2026, 5, 1, 8, 30, tzinfo=timezone.utc),
                              datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)) == [2]


# ================================
# 有界工作集与两段式替换（SLRU + 换出记录簿）
# ================================

def test_capacity_evicts_probation_lru_into_ghost(small_pool):
    for cid in (1, 2, 3, 4):
        pool.on_store_write("upsert", [_fake(cid)])
    assert pool.stats()["cards"] == 4

    pool.on_store_write("upsert", [_fake(5)])             # 超容量：换出试用层最旧者
    stats = pool.stats()
    assert stats["cards"] == 4 and stats["ghost"] == 1
    assert pool.tier_of(1) is None
    assert [c.id for c in pool.get_cards([2, 3, 4, 5])[0]] == [2, 3, 4, 5]


def test_second_read_promotes_to_protected(small_pool):
    pool.on_store_write("upsert", [_fake(1)])
    assert pool.tier_of(1) == "probation"
    pool.get_cards([1])
    assert pool.tier_of(1) == "probation"                 # 第一次读：留在试用层
    pool.get_cards([1])
    assert pool.tier_of(1) == "protected"                 # 第二次读：升保护层


def test_protected_cap_demotes_oldest(small_pool):
    for cid in (1, 2, 3, 4):
        pool.on_store_write("upsert", [_fake(cid)])
    for cid in (1, 2, 3):                                 # 三张升满保护层（配额 3）
        pool.get_cards([cid])
        pool.get_cards([cid])
    assert pool.stats()["protected"] == 3

    pool.get_cards([4])
    pool.get_cards([4])                                   # 第四张升层 → 最旧的降回试用层
    assert pool.tier_of(4) == "protected"
    assert pool.tier_of(1) == "probation"                 # 降级通道（不是换出）
    assert pool.stats()["protected"] == 3


def test_ghost_hit_refill_promotes_directly(small_pool):
    for cid in (1, 2, 3, 4):
        pool.on_store_write("upsert", [_fake(cid)])
    pool.on_store_write("upsert", [_fake(5)])             # 1 被换出 → 记录簿
    assert pool.stats()["ghost"] == 1

    pool.refill([_fake(1)])                               # 被换出过又被要回 → 直达保护层
    assert pool.tier_of(1) == "protected"
    stats = pool.stats()
    assert stats["cards"] == 4 and stats["protected"] == 1
    assert stats["ghost"] == 1                            # 补位时又挤出 2（正常腾位）


def test_ghost_is_bounded(small_pool):
    for cid in (1, 2, 3, 4):
        pool.on_store_write("upsert", [_fake(cid)])
    for cid in (5, 6, 7):                                 # 连续换出 1、2、3
        pool.on_store_write("upsert", [_fake(cid)])
    assert pool.stats()["ghost"] == 2                     # GHOST_SIZE=2：最旧记录被丢


def test_refill_without_ghost_goes_probation(small_pool):
    pool.refill([_fake(1)])
    assert pool.tier_of(1) == "probation"                 # 真·第一次见：从试用层排队


# ================================
# 纯度（E6 验收：更新函数无 IO）
# ================================

def test_update_and_read_paths_are_synchronous():
    assert not inspect.iscoroutinefunction(pool.on_store_write)
    assert not inspect.iscoroutinefunction(pool.refill)
    assert not inspect.iscoroutinefunction(pool.get_cards)
    assert not inspect.iscoroutinefunction(pool.get_card)
    assert not inspect.iscoroutinefunction(pool.by_entities)
    assert not inspect.iscoroutinefunction(pool.known_entities)
    assert not inspect.iscoroutinefunction(pool.by_time_range)
    assert not inspect.iscoroutinefunction(pool.neighbors)


# ================================
# 启动构建与增量等价（db_session 真链路）
# ================================

@pytest.mark.asyncio
async def test_build_prefers_newest_when_over_capacity(db_session, monkeypatch):
    monkeypatch.setattr(pool, "POOL_CAPACITY", 3)
    for i in range(1, 6):
        await store.create_card(db_session, kind="entity", content=f"卡{i}")
    await db_session.commit()

    report = await pool.build(db_session)
    assert report["cards"] == 3                           # 只预热最近 3 张（id 升序尾部）
    hits, missing = pool.get_cards([1, 2, 3, 4, 5])
    assert [c.id for c in hits] == [3, 4, 5] and missing == [1, 2]


@pytest.mark.asyncio
async def test_incremental_matches_rebuild(db_session):
    """重建等价性（核心不变量）：增量同步出的快表 ≡ 从同一 DB 冷启动构建出的快表。

    容量充足（无换出）时两者应逐项一致：命中集 / 实体倒排 / 线 / 时间轴 / 统计。
    """
    store.register_pool_hook(pool.on_store_write)

    a = await store.create_card(db_session, kind="entity", content="住上海", entities=["上海"])
    b = await store.create_card(db_session, kind="event", content="搬家",
                                event_time=datetime(2026, 3, 1))
    await store.commit_and_sync(db_session, upsert_cards=[a, b])
    edge = await store.create_edge(db_session, from_card_id=b.id, to_card_id=a.id, kind="led_to")
    c = await store.create_card(db_session, kind="entity", content="住北京",
                                entities=["上海", "北京"])
    await store.commit_and_sync(db_session, upsert_cards=[c], upsert_edges=[edge])

    def snapshot():
        hits, missing = pool.get_cards([a.id, b.id, c.id, 9999])
        assert missing == [9999]
        return {
            "hits": [(h.id, h.content) for h in hits],
            "entities": pool.by_entities(["上海"])[0],
            "neighbors": pool.neighbors(b.id),
            "time": pool.by_time_range(datetime(2026, 1, 1), datetime(2027, 1, 1)),
            "tiers": [pool.tier_of(x) for x in (a.id, b.id, c.id)],
            "stats": pool.stats(),
        }

    incremental = snapshot()
    pool.clear()
    rebuilt = await pool.build(db_session)
    assert rebuilt["cards"] == 3
    assert snapshot() == incremental


@pytest.mark.asyncio
async def test_supersede_flow_syncs_cards_through_pool(db_session):
    store.register_pool_hook(pool.on_store_write)
    v1 = await store.create_card(db_session, kind="entity", content="住上海")
    await store.commit_and_sync(db_session, upsert_cards=[v1])

    v2 = await store.create_card(db_session, kind="entity", content="住北京")
    edge = await store.supersede_card(db_session, new_card_id=v2.id, old_card_id=v1.id)
    await store.commit_and_sync(db_session, upsert_cards=[v2], delete_card_ids=[v1.id],
                                upsert_edges=[edge])

    assert pool.get_card(v1.id) is None                   # 旧卡出池
    assert pool.get_card(v2.id).content == "住北京"        # 新卡在册
    assert pool.stats()["ghost"] == 0                     # 取代 ≠ 换出：不入记录簿


@pytest.mark.asyncio
async def test_causal_edge_sync_via_commit_and_sync(db_session):
    """边事件在同步轨上（E6 扩展）：不重跑 build，顺线也看得见新因果线。"""
    store.register_pool_hook(pool.on_store_write)
    a = await store.create_card(db_session, kind="event", content="原因")
    b = await store.create_card(db_session, kind="event", content="结果")
    await store.commit_and_sync(db_session, upsert_cards=[a, b])

    edge = await store.create_edge(db_session, from_card_id=b.id, to_card_id=a.id,
                                   kind="caused_by")
    await store.commit_and_sync(db_session, upsert_edges=[edge])
    assert pool.neighbors(b.id) == [(a.id, "caused_by")]
    assert pool.neighbors(a.id) == [(b.id, "caused_by")]
