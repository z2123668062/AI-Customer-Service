"""extractor 抽取器单测（E8）：解析校验 / json_mode 重试 / 分段降级 / 显式通道 / 原文切块。

模型调用以可编程假件替换（extractor.client）——不触网。
所有 async 用例显式 @pytest.mark.asyncio（strict 模式）。
"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.domain import Message
from app.services.long_term_memory import extractor
from app.services.long_term_memory.extractor import CardDraft


class FakeCompletions:
    """可编程 chat.completions.create：按队列吐响应（str 或 Exception），记录调用参数。"""

    def __init__(self):
        self.queue: list = []
        self.calls: list[dict] = []

    def push(self, *items):
        self.queue.extend(items)
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item))])


@pytest.fixture
def llm(monkeypatch):
    fake = FakeCompletions()
    monkeypatch.setattr(extractor, "client",
                        SimpleNamespace(chat=SimpleNamespace(completions=fake)))
    return fake


def _payload(cards):
    return json.dumps({"cards": cards}, ensure_ascii=False)


def _msg(mid, role, content):
    return Message(id=mid, session_id="s1", role=role, content=content)


# ================================
# 解析与校验
# ================================

@pytest.mark.asyncio
async def test_extract_batch_parses_and_drops_invalid(llm):
    llm.push(_payload([
        {"kind": "entity", "subtype": "preference", "content": "用户喜欢喝咖啡",
         "entities": ["咖啡"], "event_time": None},
        {"kind": "event", "subtype": None, "content": "用户上周去了北京",
         "entities": ["北京"], "event_time": "2026-09-20"},
        {"kind": "feeling", "subtype": None, "content": "非法类型", "entities": [], "event_time": None},
        {"kind": "entity", "subtype": "心情", "content": "词表外 subtype", "entities": [], "event_time": None},
        {"kind": "entity", "subtype": "identity", "content": "", "entities": [], "event_time": None},
    ]))
    drafts = await extractor.extract_batch([_msg(1, "user", "我喜欢喝咖啡，上周还去了北京")])
    assert [d.kind for d in drafts] == ["entity", "event"]     # 三张非法卡被丢弃
    assert drafts[0].subtype == "preference" and drafts[0].entities == ("咖啡",)
    assert drafts[1].event_time == datetime(2026, 9, 20)


@pytest.mark.asyncio
async def test_extract_batch_retry_with_plain_prompt(llm):
    llm.push("这不是 json", _payload([]))
    drafts = await extractor.extract_batch([_msg(1, "user", "你好")])
    assert drafts == []                                        # 空产出 = 合法结果
    assert llm.calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in llm.calls[1]               # 重试 = 纯 prompt 模式
    assert llm.calls[0]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_extract_batch_all_segments_fail_returns_none(llm, monkeypatch):
    monkeypatch.setattr(extractor, "_SEGMENT_SIZE", 2)
    llm.push(RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom"))
    result = await extractor.extract_batch([_msg(i, "user", f"消息{i}") for i in range(1, 5)])
    assert result is None                                      # 全批失败 → 调用方不推水位


@pytest.mark.asyncio
async def test_extract_batch_partial_failure_keeps_success(llm, monkeypatch):
    monkeypatch.setattr(extractor, "_SEGMENT_SIZE", 1)
    llm.push(_payload([{"kind": "event", "content": "第一段事件", "entities": [], "event_time": None}]))
    llm.push(RuntimeError("段2挂"), RuntimeError("段2挂"))
    llm.push(_payload([{"kind": "event", "content": "第三段事件", "entities": [], "event_time": None}]))
    drafts = await extractor.extract_batch([_msg(1, "user", "a"), _msg(2, "user", "b"), _msg(3, "user", "c")])
    assert [d.content for d in drafts] == ["第一段事件", "第三段事件"]


@pytest.mark.asyncio
async def test_extract_explicit_includes_hint(llm):
    llm.push(_payload([{"kind": "entity", "subtype": "identity", "content": "用户对花生过敏",
                        "entities": ["花生"], "event_time": None}]))
    drafts = await extractor.extract_explicit([{"message_id": 1, "content": "记住我对花生过敏"}])
    assert drafts[0].content == "用户对花生过敏"
    assert "明确要求记住" in llm.calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_extract_explicit_failure_returns_none(llm):
    llm.push(RuntimeError("x"), "不是json")
    assert await extractor.extract_explicit([{"message_id": 1, "content": "记住X"}]) is None


@pytest.mark.asyncio
async def test_event_time_invalid_keeps_card(llm):
    llm.push(_payload([{"kind": "event", "content": "时间不明的旧事",
                        "entities": [], "event_time": "上周三下午"}]))
    drafts = await extractor.extract_batch([_msg(1, "user", "x")])
    assert drafts[0].event_time is None                        # 丢时间不丢卡


# ================================
# 批内去重（纯函数）
# ================================

def test_dedupe_drafts_normalizes_punct_and_case():
    drafts = [
        CardDraft(kind="event", content="用户去了北京"),
        CardDraft(kind="event", content="用户去了北京。"),        # 标点差异 = 重复
        CardDraft(kind="entity", subtype="preference", content="用户喜欢 咖啡。"),
        CardDraft(kind="entity", subtype="preference", content="用户喜欢咖啡"),
    ]
    kept = extractor.dedupe_drafts(drafts)
    assert [d.content for d in kept] == ["用户去了北京", "用户喜欢 咖啡。"]
