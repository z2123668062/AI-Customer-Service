"""writer 落库三固定动作单测（E8）：候选筛选 / 判定计划 / 落库执行（取代 + 回填）。

- db_session：独立临时库（conftest）；apply 系列只 flush——断言用同一 session 重读 / refresh；
- fake_gate：替换 gate 的两个判定函数（writer.gate 与 gate 同模块对象）——可编程、不触网；
- 事务约定：writer 不 commit（唯一提交点 = commit_and_sync），测试用 session.commit 直接落定。

所有 async 用例显式 @pytest.mark.asyncio（strict 模式）。
"""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.long_term_memory import store, writer
from app.services.long_term_memory.extractor import CardDraft


# ================================
# 夹具与工具
# ================================

@pytest.fixture
def fake_gate(monkeypatch):
    """可编程判定假件：预置结论队列（(decision, confidence)；空则回默认），记录调用参数。"""
    fake = SimpleNamespace(
        supersede_results=[],                     # 队列：每次调用弹一个
        backfill_results=[],
        supersede_calls=[],                       # (old_card, new_info)
        backfill_calls=[],                        # (event_a, event_b)
        default_supersede=("coexist", 0.9),
        default_backfill=("no", 0.9),
    )

    async def judge_supersede(old_card, new_info):
        fake.supersede_calls.append((old_card, new_info))
        decision, confidence = (fake.supersede_results.pop(0) if fake.supersede_results
                                else fake.default_supersede)
        return SimpleNamespace(decision=decision, confidence=confidence)

    async def judge_backfill(event_a, event_b):
        fake.backfill_calls.append((event_a, event_b))
        decision, confidence = (fake.backfill_results.pop(0) if fake.backfill_results
                                else fake.default_backfill)
        return SimpleNamespace(decision=decision, confidence=confidence)

    monkeypatch.setattr(writer.gate, "judge_supersede", judge_supersede)
    monkeypatch.setattr(writer.gate, "judge_backfill", judge_backfill)
    return fake


async def _mk_card(session, *, kind, content, subtype=None, entities=None):
    card = await store.create_card(session, kind=kind, content=content,
                                   subtype=subtype, entities=entities)
    await session.commit()
    return card


def _draft(kind="entity", content="新信息", subtype=None, entities=()):
    return CardDraft(kind=kind, content=content, subtype=subtype, entities=tuple(entities))


# ================================
# ① 候选筛选
# ================================

@pytest.mark.asyncio
async def test_load_candidates_by_entities_or_subtype(db_session):
    c1 = await _mk_card(db_session, kind="entity", subtype="preference",
                        content="用户喜欢喝咖啡", entities=["咖啡"])
    c2 = await _mk_card(db_session, kind="entity", subtype="identity",
                        content="用户住在上海", entities=["上海"])
    drafts = [
        _draft(subtype="relation", entities=("咖啡",)),   # 实体交集命中 c1
        _draft(subtype="preference"),                     # subtype 相同命中 c1
    ]
    cands = await writer.load_candidates(db_session, drafts=drafts)
    assert [c.id for c in cands.supersede[0]] == [c1.id]
    assert [c.id for c in cands.supersede[1]] == [c1.id]   # c2 实体/ subtype 均不命中


@pytest.mark.asyncio
async def test_load_candidates_backfill_pool_excludes_explained(db_session):
    e1 = await _mk_card(db_session, kind="event", content="事件一")
    e2 = await _mk_card(db_session, kind="event", content="事件二")
    cause = await _mk_card(db_session, kind="event", content="事件三（后补的原因）")
    await store.create_edge(db_session, from_card_id=e2.id, to_card_id=cause.id, kind="caused_by")
    await db_session.commit()

    cands = await writer.load_candidates(db_session, drafts=[_draft("event", content="新事件")])
    # desc 序 [cause, e2, e1] → 排除已有因果的 e2 → 取 top2
    assert [c.id for c in cands.backfill] == [cause.id, e1.id]


@pytest.mark.asyncio
async def test_load_candidates_marks_exact_duplicates(db_session):
    """逐字同文的草稿被标记（exact_duplicates）且不填归位候选；同批正常草稿不受影响。"""
    c1 = await _mk_card(db_session, kind="entity", subtype="preference",
                        content="用户喜欢喝咖啡", entities=["咖啡"])
    drafts = [
        _draft(subtype="preference", content="用户喜欢喝咖啡"),           # 与 c1 逐字同文
        _draft(subtype="preference", content="用户开始戒咖啡", entities=("咖啡",)),
    ]
    cands = await writer.load_candidates(db_session, drafts=drafts)
    assert cands.exact_duplicates == {0: c1.id}
    assert 0 not in cands.supersede                      # 同文草稿不填归位候选
    assert [c.id for c in cands.supersede[1]] == [c1.id]


# ================================
# ② 判定计划（事务外）
# ================================

@pytest.mark.asyncio
async def test_plan_batch_supersede_picks_highest_confidence(fake_gate):
    cands = writer.Candidates(supersede={0: [
        writer.CardSnapshot(id=10, kind="entity", subtype="identity",
                            content="用户住在上海", entities=()),
        writer.CardSnapshot(id=11, kind="entity", subtype="identity",
                            content="用户在上海工作", entities=()),
    ]})
    fake_gate.supersede_results = [("supersede", 0.8), ("supersede", 0.95)]
    plan = await writer.plan_batch(drafts=[_draft(content="用户搬到北京")], candidates=cands)
    assert plan.supersedes == {0: 11}                     # 置信度 0.95 胜出
    assert len(fake_gate.supersede_calls) == 2


@pytest.mark.asyncio
async def test_plan_batch_no_supersede_on_coexist(fake_gate):
    cands = writer.Candidates(supersede={0: [
        writer.CardSnapshot(id=10, kind="entity", subtype="preference",
                            content="用户养了猫", entities=()),
    ]})
    plan = await writer.plan_batch(drafts=[_draft(subtype="preference", content="用户又养了狗")],
                                   candidates=cands)
    assert plan.supersedes == {}                          # coexist → 新增、不连线


@pytest.mark.asyncio
async def test_plan_batch_backfill_claims_once(fake_gate):
    fake_gate.default_backfill = ("yes", 0.9)
    cands = writer.Candidates(backfill=[
        writer.CardSnapshot(id=9, kind="event", subtype=None, content="旧事件（等答案）", entities=()),
    ])
    drafts = [_draft("event", content="新事件甲"), _draft("event", content="新事件乙")]
    plan = await writer.plan_batch(drafts=drafts, candidates=cands)
    assert plan.backfills == [(9, 0)]                     # 旧卡只被第一个草稿认领
    assert len(fake_gate.backfill_calls) == 1


@pytest.mark.asyncio
async def test_plan_batch_backfill_skips_non_event(fake_gate):
    fake_gate.default_backfill = ("yes", 0.9)
    cands = writer.Candidates(backfill=[
        writer.CardSnapshot(id=9, kind="event", subtype=None, content="旧事件", entities=()),
    ])
    plan = await writer.plan_batch(drafts=[_draft(subtype="preference")], candidates=cands)
    assert plan.backfills == [] and fake_gate.backfill_calls == []


@pytest.mark.asyncio
async def test_plan_batch_stops_at_call_limit(fake_gate, monkeypatch):
    monkeypatch.setattr(writer, "_MAX_PLAN_CALLS", 2)
    cands = writer.Candidates(supersede={
        i: [writer.CardSnapshot(id=10 + i, kind="entity", subtype="identity",
                                content=f"旧卡{i}", entities=())]
        for i in range(3)
    })
    fake_gate.supersede_results = [("supersede", 0.9)] * 3
    plan = await writer.plan_batch(drafts=[_draft() for _ in range(3)], candidates=cands)
    assert len(fake_gate.supersede_calls) == 2            # 达上限即停
    assert plan.supersedes == {0: 10, 1: 11}              # 余下草稿按全新增


@pytest.mark.asyncio
async def test_plan_batch_skips_exact_duplicates(fake_gate):
    """判重草稿不参与归位/回填判定（防御层）；字段原样带入计划、同批正常草稿不受影响。"""
    fake_gate.supersede_results = [("supersede", 0.9)]
    fake_gate.default_backfill = ("yes", 0.9)
    cands = writer.Candidates(
        exact_duplicates={0: 7},
        supersede={0: [writer.CardSnapshot(id=7, kind="event", subtype=None,
                                           content="同文事件", entities=())],
                   1: [writer.CardSnapshot(id=8, kind="entity", subtype="preference",
                                           content="旧偏好", entities=())]},
        backfill=[writer.CardSnapshot(id=9, kind="event", subtype=None,
                                      content="旧事件", entities=())])
    plan = await writer.plan_batch(
        drafts=[_draft("event", content="同文事件"),
                _draft(subtype="preference", content="新偏好"),
                _draft("event", content="新事件")], candidates=cands)
    assert plan.exact_duplicates == {0: 7}
    assert plan.supersedes == {1: 8}                     # 草稿 0 跳过归位、草稿 1 正常判定
    assert plan.backfills == [(9, 2)]                    # 回填只认领非判重草稿
    assert len(fake_gate.supersede_calls) == 1 and len(fake_gate.backfill_calls) == 1


# ================================
# ③ 落库执行（短事务）
# ================================

@pytest.mark.asyncio
async def test_apply_batch_full_chain(db_session):
    old_entity = await _mk_card(db_session, kind="entity", subtype="preference",
                                content="用户喜欢喝咖啡", entities=["咖啡"])
    old_event = await _mk_card(db_session, kind="event", content="用户上周失眠")
    plan = writer.BatchPlan(
        drafts=[_draft(subtype="preference", content="用户开始戒咖啡", entities=("咖啡",)),
                _draft("event", content="用户最近工作压力大")],
        supersedes={0: old_entity.id},
        backfills=[(old_event.id, 1)],
    )
    result = await writer.apply_batch(db_session, plan=plan, session_id="s1")

    assert (result.cards_created, result.superseded,
            result.edges_created, result.drafts_dropped) == (2, 1, 2, 0)
    new_entity, new_event = result.cards
    assert new_entity.source == "passive" and new_entity.source_session_id == "s1"

    await db_session.refresh(old_entity)
    assert old_entity.status == "superseded"
    sup = await store.list_edges(db_session, kind="supersedes")
    assert [(e.from_card_id, e.to_card_id) for e in sup] == [(new_entity.id, old_entity.id)]
    cb = await store.list_edges(db_session, kind="caused_by")
    assert [(e.from_card_id, e.to_card_id) for e in cb] == [(old_event.id, new_event.id)]


@pytest.mark.asyncio
async def test_apply_batch_skips_nonactive_old(db_session):
    old = await _mk_card(db_session, kind="entity", subtype="preference", content="旧偏好")
    await store.revoke_card(db_session, old.id)
    await db_session.commit()
    plan = writer.BatchPlan(drafts=[_draft(subtype="preference", content="新偏好")],
                            supersedes={0: old.id})
    result = await writer.apply_batch(db_session, plan=plan, session_id="s1")
    assert (result.cards_created, result.superseded, result.edges_created) == (1, 0, 0)
    assert await store.list_edges(db_session, kind="supersedes") == []


@pytest.mark.asyncio
async def test_apply_batch_drops_invalid_draft(db_session):
    bad = CardDraft(kind="entity", subtype="心情", content="词表外 subtype 的草稿")
    plan = writer.BatchPlan(drafts=[bad, _draft("event", content="正常事件")])
    result = await writer.apply_batch(db_session, plan=plan, session_id="s1")
    assert result.drafts_dropped == 1 and result.cards_created == 1
    assert [c.content for c in result.cards] == ["正常事件"]   # 坏草稿不拖垮整批


@pytest.mark.asyncio
async def test_apply_batch_intercepts_exact_duplicates(db_session):
    """判重拦截：不建卡、计数入 duplicates；同批正常草稿照常落库。"""
    existing = await _mk_card(db_session, kind="entity", subtype="preference",
                              content="用户喜欢喝咖啡", entities=["咖啡"])
    plan = writer.BatchPlan(
        drafts=[_draft(subtype="preference", content="用户喜欢喝咖啡"),
                _draft("event", content="用户明天要买牛奶")],
        exact_duplicates={0: existing.id},
    )
    result = await writer.apply_batch(db_session, plan=plan, session_id="s1")
    assert result.duplicates == 1 and result.cards_created == 1
    assert [c.content for c in result.cards] == ["用户明天要买牛奶"]
    cards = await store.list_cards(db_session, kind="entity", status=store.ACTIVE)
    assert [c.id for c in cards] == [existing.id]        # 库中同文卡未被重复写入


@pytest.mark.asyncio
async def test_apply_plain_tags_source(db_session):
    drafts = [_draft("event", content="用户明天要买牛奶")]
    result = await writer.apply_plain(db_session, drafts=drafts,
                                      session_id="s1", source="explicit")
    assert result.cards_created == 1
    assert result.cards[0].source == "explicit"
    assert await store.list_edges(db_session) == []           # 无判定 → 无连线
