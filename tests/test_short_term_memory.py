"""热层（Redis 滑动窗口）单元测试——B2 基线通路的守门测试。"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core import short_term_memory as memory


@pytest.mark.asyncio
async def test_add_message(mock_redis):
    """测试添加消息——应 rpush 消息并 ltrim 窗口"""
    with patch("app.core.short_term_memory.redis_client", mock_redis):
        message = await memory.add_message(
            session_id="session_1",
            role="user",
            content="你好"
        )

        assert message.role == "user"
        assert message.content == "你好"
        mock_redis.rpush.assert_awaited_once()
        mock_redis.ltrim.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_history_empty(mock_redis):
    """测试获取历史——没有消息时返回空列表"""
    mock_redis.lrange.return_value = []

    with patch("app.core.short_term_memory.redis_client", mock_redis):
        history = await memory.get_history(session_id="session_empty")
        assert history == []


@pytest.mark.asyncio
async def test_get_history_with_messages(mock_redis):
    """测试获取历史——有消息时返回 ChatMessage 列表"""
    mock_redis.lrange.return_value = [
        json.dumps({"role": "user", "content": "你好"}),
        json.dumps({"role": "assistant", "content": "你好！有什么可以帮助你的吗？"}),
    ]

    with patch("app.core.short_term_memory.redis_client", mock_redis):
        history = await memory.get_history(session_id="session_1")

        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content == "你好"
        assert history[1].role == "assistant"


@pytest.mark.asyncio
async def test_get_history_count(mock_redis):
    """测试获取历史条数"""
    mock_redis.llen.return_value = 5

    with patch("app.core.short_term_memory.redis_client", mock_redis):
        count = await memory.get_history_count(session_id="session_1")
        assert count == 5


@pytest.mark.asyncio
async def test_undo_last_turn_success(mock_redis):
    """测试撤销最后一轮对话——有足够消息时 rpop 两次"""
    mock_redis.llen.return_value = 4
    mock_redis.rpop = AsyncMock(return_value="{}")

    with patch("app.core.short_term_memory.redis_client", mock_redis):
        await memory.undo_last_turn(session_id="session_1")
        # 应调用两次 rpop（用户+AI 各一条）
        assert mock_redis.rpop.await_count == 2


@pytest.mark.asyncio
async def test_undo_last_turn_not_enough(mock_redis):
    """测试撤销最后一轮对话——消息不足时不应 rpop"""
    mock_redis.llen.return_value = 1

    with patch("app.core.short_term_memory.redis_client", mock_redis):
        await memory.undo_last_turn(session_id="session_1")
        # llen < 2，不应执行 rpop
        mock_redis.rpop.assert_not_awaited()
