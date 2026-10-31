"""读路径单测（E9）：分档调度 / 分诊短路 / 抽线索 / 扫快表 / 缺页两级回源 / 回填 / 顺线 / trace 归因。

策略：
- 纯函数（时间词 / 反向匹配 / L2 关键词）零依赖直测；
- 全链用例恒 monkeypatch gate.judge_page_fault（读路径不真调 LLM）与 retriever._semantic_search
  （隔离 embedding/Qdrant 外设）——链路编排才是本文件的被测对象；
- 池内数据用 pool.on_store_write 直塞假卡（零 DB）；DB 侧数据走 db_session。
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.domain import Message
from app.services.long_term_memory import gate, pool, retriever, store
from app.services.long_term_memory.retriever import (
    build_l2_keywords,
    match_known_entities,
    parse_time_window,
    retrieve,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)   # 周四 12:00 UTC（周一起始：本周 = 09-28 ~ 10-05）

# 熔断测试需要真实实现（autouse fixture 会把语义路替成离线空命中）
_REAL_SEMANTIC_RAW = retriever._semantic_raw


def _fake(card_id, content=None, kind="entity", entities=(), event_time=None,
          status="active", subtype=None):
    """假卡（与 ORM Card / PoolCard 鸭子类型同形），供池直塞用。"""
    return SimpleNamespace(
        id=card_id, kind=kind, subtype=subtype, content=content or f"卡{card_id}",
        entities=list(entities), event_time=event_time, source="passive",
        created_at=None, status=status,
    )


def _gate(monkeypatch, decision="yes"):
    """替身分诊（读路径不真调 LLM）；契约与 gate.GateResult 同形。"""
    async def fake(query):
        return gate.GateResult(decision=decision, confidence=1.0, reason="test",
                               origin="prefilter", latency_ms=0.5)
    monkeypatch.setattr(gate, "judge_page_fault", fake)


def _semantic(monkeypatch, hits=()):
    """替身语义路（隔离 embedding / Qdrant 外设）。

    L1 定稿：命中路径也会调 _semantic_raw（粗排打分），两个入口都得替，
    否则 hit 用例会真连 embedding/Qdrant。
    """
    async def fake(query):
        return list(hits)

    async def fake_raw(query, *, limit):
        return list(hits)

    monkeypatch.setattr(retriever, "_semantic_search", fake)
    monkeypatch.setattr(retriever, "_semantic_raw", fake_raw)


@pytest.fixture(autouse=True)
def clean_pool(monkeypatch):
    """每测试独立快表 + 解除全局钩子 / 熔断状态；语义路默认离线（无命中）。"""
    monkeypatch.setattr(retriever, "_semantic_down_until", 0.0)

    async def _offline(query, *, limit):
        return []

    monkeypatch.setattr(retriever, "_semantic_raw", _offline)
    pool.clear()
    pool.register_fault_hook(None)
    store.register_pool_hook(None)
    yield
    pool.clear()
    pool.register_fault_hook(None)
    store.register_pool_hook(None)


# ================================
# 纯函数：时间词（规则表）
# ================================

def test_parse_time_window_none_when_no_time_word():
    assert parse_time_window("你还记得小林吗", NOW) is None


def test_parse_time_window_today_and_yesterday():
    utc = timezone.utc
    assert parse_time_window("今天怎么样", NOW) == (
        datetime(2026, 10, 1, tzinfo=utc), datetime(2026, 10, 2, tzinfo=utc))
    assert parse_time_window("昨天聊到哪了", NOW) == (
        datetime(2026, 9, 30, tzinfo=utc), datetime(2026, 10, 1, tzinfo=utc))
    assert parse_time_window("前天呢", NOW) == (
        datetime(2026, 9, 29, tzinfo=utc), datetime(2026, 9, 30, tzinfo=utc))


def test_parse_time_window_week_and_month_calendar_boundaries():
    utc = timezone.utc
    # 2026-10-01 为周四 → 本周一 = 09-28
    assert parse_time_window("本周做了什么", NOW) == (
        datetime(2026, 9, 28, tzinfo=utc), datetime(2026, 10, 5, tzinfo=utc))
    assert parse_time_window("上周", NOW) == (
        datetime(2026, 9, 21, tzinfo=utc), datetime(2026, 9, 28, tzinfo=utc))
    assert parse_time_window("本月", NOW) == (
        datetime(2026, 10, 1, tzinfo=utc), datetime(2026, 11, 1, tzinfo=utc))
    assert parse_time_window("上个月", NOW) == (
        datetime(2026, 9, 1, tzinfo=utc), datetime(2026, 10, 1, tzinfo=utc))


def test_parse_time_window_first_rule_in_table_wins_and_english():
    utc = timezone.utc
    # 多时间词共现：按规则表序（非文本位置）取首个——"今天"规则先于"上周"
    assert parse_time_window("上周到今天", NOW) == (
        datetime(2026, 10, 1, tzinfo=utc), datetime(2026, 10, 2, tzinfo=utc))
    assert parse_time_window("上周的会议", NOW) == (
        datetime(2026, 9, 21, tzinfo=utc), datetime(2026, 9, 28, tzinfo=utc))
    assert parse_time_window("what did I do last week", NOW) == (
        datetime(2026, 9, 21, tzinfo=utc), datetime(2026, 9, 28, tzinfo=utc))


# ================================
# 纯函数：反向匹配与 L2 关键词
# ================================

def test_match_known_entities_substring_and_casefold():
    known = ["小林", "python", "上海"]
    assert match_known_entities("小林最近怎么样", known) == ["小林"]
    assert match_known_entities("我在学 PYTHON", known) == ["python"]   # casefold
    assert match_known_entities("无关的话", known) == []


def test_build_l2_keywords_entity_first_then_grams():
    kws = build_l2_keywords("我打算搬家到杭州", ["搬家"])
    assert kws[0] == "搬家"                                # 实体名优先
    assert "杭州" in kws                                   # 2-gram 兜住未登记词


def test_build_l2_keywords_stop_chars_english_dedup_and_limit():
    assert build_l2_keywords("我的书", []) == []           # 停用字滤尽 → 无弹药
    assert build_l2_keywords("abcdefg test abcdefg", []) == ["abcdefg", "test"]  # 英文≥3 + 去重
    kws = build_l2_keywords("一二三四五六七八九十甲乙丙丁戊己", [])
    assert len(kws) <= 8                                   # 上限截断


# ================================
# 分诊短路（skip）
# ================================

@pytest.mark.asyncio
async def test_full_skip_touches_neither_pool_nor_db(db_session, monkeypatch):
    _gate(monkeypatch, decision="no")

    def boom(*args, **kwargs):
        raise AssertionError("skip 档不应触碰快表 / 回源")
    monkeypatch.setattr(pool, "known_entities", boom)
    monkeypatch.setattr(pool, "by_entities", boom)
    monkeypatch.setattr(pool, "by_time_range", boom)

    result = await retrieve("今天天气怎么样", session=db_session, now=NOW)
    assert result.candidates == []
    assert result.trace["triage"] == "skip"
    assert result.trace["gate"]["decision"] == "no"
    assert result.trace["merged_count"] == 0


# ================================
# full 档：快表 hit（本地有收获，不回源）
# ================================

@pytest.mark.asyncio
async def test_full_hit_entity_route_does_not_source(db_session, monkeypatch):
    _gate(monkeypatch)
    pool.on_store_write("upsert", [_fake(1, content="小林是同事", entities=("小林",))])

    async def boom(*args, **kwargs):
        raise AssertionError("hit 档不应走 L1 扫描 / 语义回源")
    monkeypatch.setattr(store, "list_cards", boom)
    monkeypatch.setattr(retriever, "_semantic_search", boom)

    result = await retrieve("小林最近怎么样", session=db_session, now=NOW)
    assert result.trace["triage"] == "hit"
    assert result.trace["hit_cards"] == [1]
    [c] = result.candidates
    assert c.ref_id == 1 and c.routes == frozenset({"entity"})


@pytest.mark.asyncio
async def test_full_hit_time_route(db_session, monkeypatch):
    _gate(monkeypatch)
    pool.on_store_write("upsert", [
        _fake(1, kind="event", content="体检", event_time=datetime(2026, 10, 1, 9, 0)),
    ])

    result = await retrieve("今天做了什么", session=db_session, now=NOW)
    assert result.trace["triage"] == "hit"
    assert result.trace["pool_hits"] == {"entity": 0, "time": 1}
    [c] = result.candidates
    assert c.routes == frozenset({"time"})


@pytest.mark.asyncio
async def test_full_hit_same_card_merges_routes_attribution_by_strongest(db_session, monkeypatch):
    _gate(monkeypatch)
    pool.on_store_write("upsert", [
        _fake(1, kind="event", entities=("小林",), event_time=datetime(2026, 10, 1, 9, 0)),
    ])

    result = await retrieve("小林今天怎么样", session=db_session, now=NOW)
    [c] = result.candidates
    assert c.routes == frozenset({"entity", "time"})       # 证据链并集
    assert result.trace["candidates"] == {"entity": 1, "semantic": 0, "time": 0, "line": 0, "raw": 0}
    assert result.trace["merged_count"] == 1               # 按最强入口归因（entity）


# ================================
# full 档：缺页 fault → L1（实体 / 时间 / 语义）
# ================================

@pytest.mark.asyncio
async def test_full_fault_l1_entity_via_quoted_heuristic(db_session, monkeypatch):
    """池空时抽实体靠引号启发式——L1 实体路的真实触发场景。"""
    _gate(monkeypatch)
    _semantic(monkeypatch)
    card = await store.create_card(db_session, kind="entity", content="小林是同事",
                                   entities=["小林"], subtype="relation")
    await db_session.commit()

    result = await retrieve('你还记得"小林"吗', session=db_session, now=NOW)
    t = result.trace
    assert t["triage"] == "fault" and t["fault_level"] == "L1"
    assert t["entities"] == ["小林"]                       # 引号启发式供词
    assert t["l1"]["entity"] == 1 and t["l1"]["refilled"] == 1
    [c] = result.candidates
    assert c.ref_id == card.id and c.routes == frozenset({"entity"})
    assert pool.get_card(card.id) is not None              # 回填已入池


@pytest.mark.asyncio
async def test_full_fault_l1_time_naive_db_value_coerced(db_session, monkeypatch):
    """SQLite 读回的 naive event_time 经 _coerce_utc 与 aware 窗口对齐（D24 修复）。"""
    _gate(monkeypatch)
    _semantic(monkeypatch)
    card = await store.create_card(db_session, kind="event", content="去医院体检",
                                   event_time=datetime(2026, 10, 1, 8, 30))
    await db_session.commit()

    result = await retrieve("今天有什么安排来着", session=db_session, now=NOW)
    t = result.trace
    assert t["fault_level"] == "L1" and t["l1"]["time"] == 1
    [c] = result.candidates
    assert c.ref_id == card.id and c.routes == frozenset({"time"})


@pytest.mark.asyncio
async def test_full_fault_l1_semantic_carries_score(db_session, monkeypatch):
    _gate(monkeypatch)
    card = await store.create_card(db_session, kind="entity", content="喜欢喝美式")
    await db_session.commit()
    _semantic(monkeypatch, [(card.id, 0.85)])

    result = await retrieve("我平时喝什么咖啡", session=db_session, now=NOW)
    t = result.trace
    assert t["fault_level"] == "L1" and t["l1"]["semantic"] == 1
    [c] = result.candidates
    assert c.routes == frozenset({"semantic"}) and c.semantic_score == 0.85


@pytest.mark.asyncio
async def test_semantic_breaker_skips_repeated_embed_cost(db_session, monkeypatch):
    """语义路熔断：一次失败后冷窗内不再 embed（Qdrant 挂掉时不重复白付成本）。"""
    calls = {"n": 0}

    async def boom(texts):
        calls["n"] += 1
        raise RuntimeError("embedding down (injected)")
    monkeypatch.setattr(store, "embed_texts", boom)
    monkeypatch.setattr(retriever, "_semantic_raw", _REAL_SEMANTIC_RAW)   # 用真实现测熔断

    assert await retriever._semantic_search("第一次查询") == []
    assert await retriever._semantic_search("第二次查询") == []
    assert calls["n"] == 1                                 # 第二次直接熔断，不再白付 embed


@pytest.mark.asyncio
async def test_source_scan_respects_cost_limit(db_session, monkeypatch):
    """回源成本上限：扫描恒带 limit=SOURCE_SCAN_LIMIT 与 desc（最近优先）。"""
    _gate(monkeypatch)
    _semantic(monkeypatch)
    seen = {}
    real_list_cards = store.list_cards

    async def spy(session, **kwargs):
        seen.update(kwargs)
        return await real_list_cards(session, **kwargs)
    monkeypatch.setattr(store, "list_cards", spy)

    await retrieve('你还记得"小林"吗', session=db_session, now=NOW)
    assert seen["limit"] == settings.SOURCE_SCAN_LIMIT and seen["desc"] is True


# ================================
# full 档：fault → L2（原文兜底）与回填可观测
# ================================

@pytest.mark.asyncio
async def test_full_fault_l2_raw_candidate_when_l1_empty(db_session, monkeypatch):
    _gate(monkeypatch)
    _semantic(monkeypatch)
    msg = Message(session_id="s1", role="user", content="我打算十月二十号搬家到杭州")
    db_session.add(msg)
    await db_session.commit()

    result = await retrieve('你还记得"搬家"的事吗', session=db_session, now=NOW)
    t = result.trace
    assert t["triage"] == "fault" and t["fault_level"] == "L2"
    assert t["l2"]["messages"] == 1
    [c] = result.candidates
    assert c.kind == "raw" and c.source == "raw" and c.ref_id == msg.id
    assert c.routes == frozenset({"raw"})


@pytest.mark.asyncio
async def test_full_fault_l2_no_keywords_returns_empty(db_session, monkeypatch):
    _gate(monkeypatch)
    _semantic(monkeypatch)

    result = await retrieve("嗯，好", session=db_session, now=NOW)   # 全停用字 → 无弹药
    assert result.trace["fault_level"] == "L2"
    assert result.trace["l2"]["messages"] == 0
    assert result.candidates == []


@pytest.mark.asyncio
async def test_backfill_makes_second_query_a_hit(db_session, monkeypatch):
    """回填可观测（E9 验收）：一次 fault 回源后，同题第二问直接快表命中。"""
    _gate(monkeypatch)
    _semantic(monkeypatch)
    card = await store.create_card(db_session, kind="entity", content="小林是同事",
                                   entities=["小林"], subtype="relation")
    await db_session.commit()

    first = await retrieve('你还记得"小林"吗', session=db_session, now=NOW)
    assert first.trace["triage"] == "fault" and first.trace["fault_level"] == "L1"

    second = await retrieve("小林最近怎么样", session=db_session, now=NOW)
    assert second.trace["triage"] == "hit"
    assert second.trace["hit_cards"] == [card.id]
    assert second.trace["stage_ms"]["triage"] >= 0


# ================================
# full 档：顺线一跳（因果线；池内免费 / 池外预算预取 / 跳过版本边）
# ================================

@pytest.mark.asyncio
async def test_line_expand_collects_pool_neighbor_free(db_session, monkeypatch):
    _gate(monkeypatch)
    pool.on_store_write("upsert", [
        _fake(1, content="小林是同事", entities=("小林",)),
        _fake(2, kind="event", content="小林换工作了"),
    ])
    pool.on_store_write("edge_upsert", [(2, 1, "led_to")])

    result = await retrieve("小林最近怎么样", session=db_session, now=NOW)
    by_id = {c.ref_id: c for c in result.candidates}
    assert set(by_id) == {1, 2}
    assert by_id[2].routes == frozenset({"line"}) and by_id[2].line_kind == "led_to"
    assert result.trace["line"] == {"collected": 1, "fetched": 0}


@pytest.mark.asyncio
async def test_line_expand_skips_supersedes_edges(db_session, monkeypatch):
    _gate(monkeypatch)
    pool.on_store_write("upsert", [_fake(1, entities=("小林",)), _fake(2, content="旧版本")])
    pool.on_store_write("edge_upsert", [(2, 1, "supersedes")])   # 版本边不是证据扩展

    result = await retrieve("小林最近怎么样", session=db_session, now=NOW)
    assert [c.ref_id for c in result.candidates] == [1]


@pytest.mark.asyncio
async def test_line_expand_fetches_out_of_pool_neighbor_from_db(db_session, monkeypatch):
    _gate(monkeypatch)
    _semantic(monkeypatch)
    a = await store.create_card(db_session, kind="event", content="搬到杭州",
                                entities=["杭州"], event_time=datetime(2026, 9, 20, 10, 0))
    b = await store.create_card(db_session, kind="event", content="找房子", entities=["小王"])
    await store.create_edge(db_session, from_card_id=a.id, to_card_id=b.id, kind="caused_by")
    await db_session.commit()

    result = await retrieve('你还记得"杭州"吗', session=db_session, now=NOW)
    by_id = {c.ref_id: c for c in result.candidates}
    assert by_id[a.id].routes == frozenset({"entity"})
    assert by_id[b.id].routes == frozenset({"line"})
    assert by_id[b.id].line_kind == "caused_by"
    assert result.trace["line"] == {"collected": 0, "fetched": 1}
    assert pool.get_card(b.id) is not None                 # 预取回填已入池
    assert pool.neighbors(a.id) == [(b.id, "caused_by")]   # 边同批装回镜像


# ================================
# trace 契约：全字段恒定出现
# ================================

@pytest.mark.asyncio
async def test_trace_skeleton_fields_always_present(db_session, monkeypatch):
    _gate(monkeypatch, decision="no")
    result = await retrieve("闲聊", session=db_session, now=NOW)
    skeleton = {
        "triage", "gate", "entities", "entities_missing", "time_window",
        "hit_cards", "pool_hits", "fault_level", "l1", "l2", "line",
        "candidates", "merged_count", "stage_ms",
    }
    assert skeleton <= set(result.trace)
    assert set(result.trace["stage_ms"]) == {"triage", "retrieval"}
    assert "has_hook" not in result.trace                  # D24 修订：并入 gate 详情
