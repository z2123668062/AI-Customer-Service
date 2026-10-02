"""consolidator 写入编排单测（E8；D23 全局水位重构）：触发判断 / 批抽取 / 显式通道 / 启动补偿 / 空闲收口。

- stack：独立库（db_maker）+ 假抽取（FakeExtract）+ 假 Redis + 独立 journal 临时文件
  （journal._journal_file 注入）+ 判定旁路（GATE_ENABLED=False → fail-safe，不触网）
  + QDRANT_ENABLED=False（提交不等向量面）；
- 后台任务模式真实：note_activity / startup_scan / reap 拉起任务后，用 wait_idle() 等完再断言；
- 水位断言**直读 journal 文件**（read_watermark——真相在文件，不经内存缓存）；
- 阈值固定 EXTRACT_WATERMARK_ROUNDS=4（= 8 条消息触发），空闲收口用 40 分钟前 = 静默超时。

所有 async 用例显式 @pytest.mark.asyncio（strict 模式）。
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.core.config import settings
from app.models.domain import Message
from app.services.long_term_memory import consolidator, explicit, gate, journal, store
from app.services.long_term_memory.extractor import CardDraft


# ================================
# 夹具与工具
# ================================

class FakeExtract:
    """可编程抽取假件：记录批/显式调用（消息 id 列表）；结果为值 / callable / None。"""

    def __init__(self):
        self.batch_calls: list[list[int]] = []
        self.explicit_calls: list[list[int]] = []
        self.batch_result = []        # list[CardDraft] / None / callable(messages)
        self.explicit_result = []     # list[CardDraft] / None / callable(entries)

    async def extract_batch(self, messages):
        self.batch_calls.append([m.id for m in messages])
        return self.batch_result(messages) if callable(self.batch_result) else self.batch_result

    async def extract_explicit(self, entries):
        self.explicit_calls.append([e["message_id"] for e in entries])
        return self.explicit_result(entries) if callable(self.explicit_result) else self.explicit_result


@pytest_asyncio.fixture
async def stack(db_maker, fake_redis, monkeypatch, tmp_path):
    """写路径测试栈：独立库 + 假抽取 + 假 Redis + 独立 journal 文件 + 判定旁路（不触网）。"""
    fake_llm = FakeExtract()
    monkeypatch.setattr(consolidator, "SessionFactory", db_maker)
    monkeypatch.setattr(consolidator.extractor, "extract_batch", fake_llm.extract_batch)
    monkeypatch.setattr(consolidator.extractor, "extract_explicit", fake_llm.extract_explicit)
    monkeypatch.setattr(explicit, "_redis", fake_redis)
    monkeypatch.setattr(journal, "_journal_file", str(tmp_path / "extract_journal.log"))
    monkeypatch.setattr(settings, "WRITE_MODE", "cards")
    monkeypatch.setattr(settings, "SUPERSEDE_ENABLED", True)
    monkeypatch.setattr(settings, "GATE_ENABLED", False)     # 旁路（fail-safe：coexist / no）
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)
    monkeypatch.setattr(settings, "EXTRACT_WATERMARK_ROUNDS", 4)
    monkeypatch.setattr(settings, "EXTRACT_IDLE_TIMEOUT_MINUTES", 30)
    consolidator.reset_for_tests()
    yield SimpleNamespace(llm=fake_llm, redis=fake_redis)
    await consolidator.wait_idle()
    consolidator.reset_for_tests()


async def _append(maker, session_id: str, content: str, role: str = "user") -> int:
    async with maker() as session:
        msg = Message(session_id=session_id, role=role, content=content)
        session.add(msg)
        await session.flush()
        mid = int(msg.id)
        await session.commit()
    return mid


async def _append_bulk(maker, session_id: str, count: int, *,
                       role: str = "user", prefix: str = "消息",
                       at: datetime | None = None) -> int:
    """一次会话插入 count 条（返回最后一条 id）；at 非空 = 显式旧时间（空闲收口测试）。"""
    async with maker() as session:
        last = None
        for i in range(count):
            last = Message(session_id=session_id, role=role, content=f"{prefix}{i + 1}")
            if at is not None:
                last.created_at = at
            session.add(last)
        await session.flush()
        mid = int(last.id)
        await session.commit()
    return mid


def _watermark():
    """直读 journal 文件水位（真相在文件；None = 尚未抽过）。"""
    return journal.read_watermark()


async def _cards(maker, *, status=None, **kw):
    async with maker() as session:
        return await store.list_cards(session, status=status, **kw)


# ================================
# ① 触发判断（全局口径，D23）
# ================================

@pytest.mark.asyncio
async def test_rounds_trigger_advances_watermark(stack, db_maker):
    await _append_bulk(db_maker, "s1", 8, prefix="闲聊")
    stack.llm.batch_result = [CardDraft(kind="entity", subtype="identity", content="用户住在上海")]
    assert await consolidator.note_activity(session_id="s1") == "rounds"
    await consolidator.wait_idle()

    assert stack.llm.batch_calls == [list(range(1, 9))]
    assert _watermark() == 8
    assert len(await _cards(db_maker)) == 1
    report = await consolidator.lag_report()
    assert report["watermark"] == 8 and report["lag_messages"] == 0
    assert report["last_record"]["event"] == "抽水完成"


@pytest.mark.asyncio
async def test_below_threshold_no_trigger(stack, db_maker):
    await _append_bulk(db_maker, "s1", 6, prefix="日常")
    assert await consolidator.note_activity(session_id="s1") is None
    await consolidator.wait_idle()
    assert stack.llm.batch_calls == []
    assert _watermark() is None                                  # 未抽过：journal 无记录


@pytest.mark.asyncio
async def test_important_statement_triggers_early(stack, db_maker):
    mid = await _append(db_maker, "s1", "我搬到北京了")
    stack.llm.batch_result = [CardDraft(kind="entity", subtype="identity", content="用户住在北京")]
    assert await consolidator.note_activity(session_id="s1") == "important"
    await consolidator.wait_idle()
    assert mid == 1
    assert _watermark() == 1


@pytest.mark.asyncio
async def test_global_scope_mixes_sessions(stack, db_maker):
    """D23 全局水位：跨会话消息进同一批（单指针——不按会话分片）。"""
    await _append_bulk(db_maker, "s1", 4, prefix="A")
    await _append_bulk(db_maker, "s2", 4, prefix="B")
    stack.llm.batch_result = []
    assert await consolidator.note_activity(session_id="s2") == "rounds"
    await consolidator.wait_idle()
    assert stack.llm.batch_calls == [[1, 2, 3, 4, 5, 6, 7, 8]]   # 一锅端（跨会话混批）
    assert _watermark() == 8


# ================================
# ② 显式通道
# ================================

@pytest.mark.asyncio
async def test_explicit_channel_creates_card_without_watermark(stack, db_maker):
    mid = await _append(db_maker, "s1", "提醒我明天买牛奶")
    await explicit.push_explicit(session_id="s1", message_id=mid, content="提醒我明天买牛奶")
    stack.llm.explicit_result = [CardDraft(kind="event", content="用户明天要买牛奶")]
    assert await consolidator.note_activity(session_id="s1") == "explicit"
    await consolidator.wait_idle()

    cards = await _cards(db_maker, source_session_id="s1")
    assert len(cards) == 1 and cards[0].source == "explicit"
    assert stack.llm.explicit_calls == [[mid]]
    assert await explicit.list_pending() == []                   # 成功即清账
    assert _watermark() is None                                  # 显式单抽不推水位（批通道负责）


@pytest.mark.asyncio
async def test_explicit_failure_keeps_cache(stack, db_maker):
    mid = await _append(db_maker, "s1", "别忘了下周三交房租")
    await explicit.push_explicit(session_id="s1", message_id=mid, content="别忘了下周三交房租")
    stack.llm.explicit_result = None
    assert await consolidator.note_activity(session_id="s1") == "explicit"
    await consolidator.wait_idle()

    assert [e["message_id"] for e in await explicit.list_pending()] == [mid]   # 保留重试
    assert await _cards(db_maker) == []


@pytest.mark.asyncio
async def test_explicit_message_not_reextracted_by_batch(stack, db_maker):
    mid = await _append(db_maker, "s1", "记住我的车钥匙放玄关了")
    await explicit.push_explicit(session_id="s1", message_id=mid, content="记住我的车钥匙放玄关了")
    stack.llm.explicit_result = [CardDraft(kind="event", content="用户把车钥匙放在玄关")]
    assert await consolidator.note_activity(session_id="s1") == "explicit"
    await consolidator.wait_idle()

    await _append_bulk(db_maker, "s1", 7, prefix="闲聊")         # 凑满 8 条触发批抽取
    stack.llm.batch_result = []
    assert await consolidator.note_activity(session_id="s1") == "rounds"
    await consolidator.wait_idle()

    assert stack.llm.batch_calls == [[2, 3, 4, 5, 6, 7, 8]]      # 已单抽的 1 不在批内
    assert _watermark() == 8


@pytest.mark.asyncio
async def test_batch_fallback_covers_cached_explicit(stack, db_maker):
    """缓存未清（单抽尚未来得及跑）时批兜底覆盖——两条通道不重复建卡。"""
    mid = await _append(db_maker, "s1", "记住我的车钥匙放玄关了")
    await explicit.push_explicit(session_id="s1", message_id=mid, content="记住我的车钥匙放玄关了")
    await _append_bulk(db_maker, "s1", 7, prefix="闲聊")         # rounds 优先级高于 explicit
    stack.llm.batch_result = [CardDraft(kind="event", content="用户把车钥匙放在玄关")]
    assert await consolidator.note_activity(session_id="s1") == "rounds"
    await consolidator.wait_idle()

    in_batch = any(mid in call for call in stack.llm.batch_calls)
    in_explicit = stack.llm.explicit_calls != []
    assert in_batch ^ in_explicit                                # 恰好一条通道抽取
    assert await explicit.list_pending() == []                   # 缓存已清账
    assert _watermark() == 8


@pytest.mark.asyncio
async def test_important_overlap_yields_to_explicit(stack, db_maker):
    """important 与 explicit 重叠（"记住：我住在…"）：让位单抽保标签、批延后（不拉）。"""
    mid = await _append(db_maker, "s1", "记住：我住在杭州")
    await explicit.push_explicit(session_id="s1", message_id=mid, content="记住：我住在杭州")
    stack.llm.explicit_result = [CardDraft(kind="entity", subtype="identity", content="用户住在杭州")]
    assert await consolidator.note_activity(session_id="s1") == "explicit"
    await consolidator.wait_idle()

    assert stack.llm.batch_calls == []                           # 批被延后（不拉）
    assert stack.llm.explicit_calls == [[mid]]                   # 单抽处理
    cards = await _cards(db_maker, source_session_id="s1")
    assert len(cards) == 1 and cards[0].source == "explicit"     # ★ 标签保住
    assert await explicit.list_pending() == []
    assert _watermark() is None                                  # 显式不推水位（批通道负责）


# ================================
# ③ 失败与重试（journal 失败行 + 水位不动）
# ================================

@pytest.mark.asyncio
async def test_batch_failure_leaves_watermark_and_records_journal(stack, db_maker):
    await _append_bulk(db_maker, "s1", 8, prefix="闲聊")
    stack.llm.batch_result = None                                # 抽取失败
    assert await consolidator.note_activity(session_id="s1") == "rounds"
    await consolidator.wait_idle()

    rec = journal.read_last_record()
    assert rec["event"] == "抽水失败" and rec["watermark"] == 0    # 水位不动（仅失败留痕）
    assert await _cards(db_maker) == []

    stack.llm.batch_result = [CardDraft(kind="event", content="用户昨天去看了电影")]
    assert await consolidator.backfill() == 1                    # 自动补抽（全局）
    assert _watermark() == 8
    assert journal.read_last_record()["event"] == "抽水完成"


@pytest.mark.asyncio
async def test_retrigger_does_not_duplicate_cards(stack, db_maker):
    await _append_bulk(db_maker, "s1", 8, prefix="闲聊")
    stack.llm.batch_result = [CardDraft(kind="entity", subtype="preference", content="用户喜欢喝咖啡")]
    assert await consolidator.note_activity(session_id="s1") == "rounds"
    await consolidator.wait_idle()
    assert len(await _cards(db_maker)) == 1

    assert await consolidator.backfill() == 1                    # 无新消息：一轮空转即停
    assert len(stack.llm.batch_calls) == 1
    assert len(await _cards(db_maker)) == 1


@pytest.mark.asyncio
async def test_backfill_drains_multiple_batches(stack, db_maker, monkeypatch):
    monkeypatch.setattr(consolidator, "_BATCH_LIMIT", 5)
    await _append_bulk(db_maker, "s1", 12)
    stack.llm.batch_result = []
    assert await consolidator.backfill() == 3                    # 5 + 5 + 2
    assert _watermark() == 12
    assert [len(c) for c in stack.llm.batch_calls] == [5, 5, 2]


# ================================
# ④ 启动补偿（journal 三态：画线 / 补抽 / 自检对齐）
# ================================

@pytest.mark.asyncio
async def test_startup_scan_first_run_draws_line(stack, db_maker):
    """首启（journal 无记录）：画线跳过存量，不触发抽取。"""
    await _append_bulk(db_maker, "s1", 2, prefix="存量")
    stack.llm.batch_result = []
    assert (await consolidator.startup_scan())["action"] == "first_run_line"
    await consolidator.wait_idle()
    assert stack.llm.batch_calls == []                           # 存量跳过（不抽）
    assert _watermark() == 2                                     # 画线行落文件
    assert journal.read_last_record()["event"] == "首启画线"
    assert await consolidator.note_activity(session_id="s1") is None   # 画线后无落后


@pytest.mark.asyncio
async def test_startup_scan_backfills_when_lagging(stack, db_maker):
    """水位落后：启动拉起补抽（只抽水位之后的消息）。"""
    journal.append_record(journal.format_record(event="抽水完成", watermark=1, detail="原文=1条"))
    await _append_bulk(db_maker, "s1", 3, prefix="消息")          # id 1..3
    stack.llm.batch_result = []
    report = await consolidator.startup_scan()
    assert report["action"] == "backfill" and report["watermark"] == 1
    await consolidator.wait_idle()
    assert stack.llm.batch_calls == [[2, 3]]
    assert _watermark() == 3


@pytest.mark.asyncio
async def test_startup_scan_self_heals_overrun(stack, db_maker):
    """水位超前（换库/删库）：自检对齐为最新消息 id，不抽。"""
    journal.append_record(journal.format_record(event="抽水完成", watermark=99, detail="旧环境残留"))
    await _append_bulk(db_maker, "s1", 2, prefix="消息")
    stack.llm.batch_result = []
    assert (await consolidator.startup_scan())["action"] == "self_heal"
    await consolidator.wait_idle()
    assert _watermark() == 2
    assert stack.llm.batch_calls == []


# ================================
# ⑤ 空闲收口（全局静默计时 = 最新消息时间）
# ================================

@pytest.mark.asyncio
async def test_reap_idle_triggers_when_quiet_and_lagging(stack, db_maker):
    old = datetime.now(timezone.utc) - timedelta(minutes=40)
    await _append_bulk(db_maker, "s1", 2, prefix="静默", at=old)
    stack.llm.batch_result = []
    assert await consolidator.reap_idle_sessions() is True
    await consolidator.wait_idle()
    assert _watermark() == 2


@pytest.mark.asyncio
async def test_reap_idle_skips_when_active(stack, db_maker):
    await _append_bulk(db_maker, "s1", 2, prefix="活跃")          # 最新消息 = 现在
    stack.llm.batch_result = []
    assert await consolidator.reap_idle_sessions() is False
    await consolidator.wait_idle()
    assert stack.llm.batch_calls == []


@pytest.mark.asyncio
async def test_reap_idle_skips_when_caught_up(stack, db_maker):
    old = datetime.now(timezone.utc) - timedelta(minutes=40)
    await _append_bulk(db_maker, "s1", 2, prefix="静默", at=old)
    journal.append_record(journal.format_record(event="抽水完成", watermark=2, detail="已追平"))
    assert await consolidator.reap_idle_sessions() is False       # 无落后：不收口
    await consolidator.wait_idle()
    assert stack.llm.batch_calls == []


# ================================
# ⑥ 档位与端到端
# ================================

@pytest.mark.asyncio
async def test_write_disabled_short_circuits(stack, db_maker, monkeypatch):
    monkeypatch.setattr(settings, "WRITE_MODE", "off")
    await _append_bulk(db_maker, "s1", 8)
    assert await consolidator.note_activity(session_id="s1") is None
    assert (await consolidator.startup_scan())["action"] == "off"
    assert await consolidator.reap_idle_sessions() is False
    assert await consolidator.backfill() == 0
    await consolidator.wait_idle()
    assert stack.llm.batch_calls == []


@pytest.mark.asyncio
async def test_raw_chunks_mode_builds_plain_cards(stack, db_maker, monkeypatch):
    monkeypatch.setattr(settings, "WRITE_MODE", "raw_chunks")
    await _append_bulk(db_maker, "s1", 8, prefix="消息内容")
    assert await consolidator.note_activity(session_id="s1") == "rounds"
    await consolidator.wait_idle()

    assert stack.llm.batch_calls == []                           # B4′：零抽取零判定
    cards = await _cards(db_maker)
    assert cards and all(c.kind == "event" for c in cards)
    assert all(c.content.startswith(("用户：", "助手：")) for c in cards)
    assert _watermark() == 8


@pytest.mark.asyncio
async def test_supersede_chain_end_to_end(stack, db_maker, monkeypatch):
    async def judge_supersede(old_card, new_info):
        return SimpleNamespace(decision="supersede", confidence=0.95)

    monkeypatch.setattr(gate, "judge_supersede", judge_supersede)

    # 第一轮：建立旧卡（"我住在" 命中重要声明 → 提前触发）
    await _append(db_maker, "s1", "我住在上海")
    stack.llm.batch_result = [CardDraft(kind="entity", subtype="identity",
                                        content="用户住在上海", entities=("上海",))]
    assert await consolidator.note_activity(session_id="s1") == "important"
    await consolidator.wait_idle()

    # 第二轮：搬家 → 取代（"我搬到" 命中重要声明）
    await _append(db_maker, "s1", "我搬到北京了")
    stack.llm.batch_result = [CardDraft(kind="entity", subtype="identity",
                                        content="用户住在北京", entities=("北京",))]
    assert await consolidator.note_activity(session_id="s1") == "important"
    await consolidator.wait_idle()

    old, new = await _cards(db_maker, kind="entity", subtype="identity")
    assert (old.content, old.status) == ("用户住在上海", "superseded")
    assert (new.content, new.status) == ("用户住在北京", "active")
    async with db_maker() as session:
        edges = await store.list_edges(session, kind="supersedes")
    assert [(e.from_card_id, e.to_card_id) for e in edges] == [(new.id, old.id)]
    assert _watermark() == 2
