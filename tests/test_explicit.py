"""explicit 显式通道单测（E8）：识别正则 / Redis 暂存与清理（FakeRedis 真实现 List 语义）。

注意：explicit 模块复用 short_term_memory.redis_client；测试以 monkeypatch 替换模块内
_redis 别名（生产单例不参与测试）。所有 async 用例显式 @pytest.mark.asyncio（strict 模式）。
"""
import pytest

from app.services.long_term_memory import explicit


@pytest.fixture
def redis(fake_redis, monkeypatch):
    monkeypatch.setattr(explicit, "_redis", fake_redis)
    return fake_redis


# ================================
# 识别（正则）
# ================================

@pytest.mark.parametrize("text", [
    "记住我喜欢喝美式咖啡",
    "记一下：周五要体检",
    "帮我记着妈妈的生日是10月3日",
    "别忘了下周交房租",
    "提醒我明天买牛奶",
])
def test_is_explicit_hits(text):
    assert explicit.is_explicit(text) is True


@pytest.mark.parametrize("text", ["今天天气不错", "帮我看看这段代码", "记", "12345", ""])
def test_is_explicit_misses(text):
    assert explicit.is_explicit(text) is False


# ================================
# 暂存 / 取回 / 清理
# ================================

@pytest.mark.asyncio
async def test_push_list_and_expire(redis):
    assert await explicit.push_explicit(session_id="s1", message_id=1, content="记住我住上海") is True
    await explicit.push_explicit(session_id="s2", message_id=2, content="记住我住北京")

    entries = await explicit.list_pending()
    assert [e["message_id"] for e in entries] == [1, 2]
    assert [e["message_id"] for e in await explicit.list_pending("s1")] == [1]
    assert redis.expires[explicit.CACHE_KEY] == explicit.CACHE_TTL_SECONDS


@pytest.mark.asyncio
async def test_push_trims_to_max(redis):
    for i in range(explicit.CACHE_MAX + 3):
        await explicit.push_explicit(session_id="s1", message_id=i, content=f"记住第{i}条")
    entries = await explicit.list_pending()
    assert len(entries) == explicit.CACHE_MAX
    assert entries[0]["message_id"] == 3                      # 裁旧留新


@pytest.mark.asyncio
async def test_clear_entries_removes_targets_only(redis):
    for i in (1, 2, 3):
        await explicit.push_explicit(session_id="s1", message_id=i, content=f"记住{i}")
    assert await explicit.clear_entries([2, 999]) == 1        # 不存在的目标不计数
    assert [e["message_id"] for e in await explicit.list_pending()] == [1, 3]

    assert await explicit.clear_entries([1, 3]) == 2
    assert await explicit.list_pending() == []
    assert explicit.CACHE_KEY not in redis.store              # 清空后键被删


@pytest.mark.asyncio
async def test_push_failure_degrades_silently(redis):
    redis.fail = True
    assert await explicit.push_explicit(session_id="s1", message_id=1, content="记住X") is False
    assert await explicit.list_pending() == []
    assert await explicit.clear_entries([1]) == 0


@pytest.mark.asyncio
async def test_corrupted_entries_skipped(redis):
    redis.store[explicit.CACHE_KEY] = ["not-json", '{"no_message_id": 1}']
    await explicit.push_explicit(session_id="s1", message_id=7, content="记住7")
    entries = await explicit.list_pending()
    assert [e["message_id"] for e in entries] == [7]
