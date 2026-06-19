import pytest
from unittest.mock import patch, AsyncMock
from app.services import session_service


@pytest.mark.asyncio
async def test_create_session(mock_redis):
    """测试创建会话——应返回 session_id 和 title"""
    with patch("app.services.session_service.redis_client", mock_redis):
        result = await session_service.create_session(user_id=1, title="测试对话")

        assert "session_id" in result
        assert result["title"] == "测试对话"
        # 验证 Redis hset 被调用
        mock_redis.hset.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_sessions_empty(mock_redis):
    """测试列出会话——没有会话时返回空列表"""
    mock_redis.hgetall.return_value = {}

    with patch("app.services.session_service.redis_client", mock_redis):
        result = await session_service.list_sessions(user_id=1)

        assert result == []


@pytest.mark.asyncio
async def test_list_sessions_with_data(mock_redis):
    """测试列出会话——有会话时返回排序后的列表"""
    import json
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    mock_redis.hgetall.return_value = {
        "session_1": json.dumps({
            "title": "对话1",
            "created_at": (now - timedelta(hours=1)).isoformat()  # 更早
        }),
        "session_2": json.dumps({
            "title": "对话2",
            "created_at": now.isoformat()  # 更新
        }),
    }

    with patch("app.services.session_service.redis_client", mock_redis):
        result = await session_service.list_sessions(user_id=1)

        assert len(result) == 2
        assert result[0]["title"] == "对话2"  # 时间更新的排前面


@pytest.mark.asyncio
async def test_update_session_title_success(mock_redis):
    """测试更新会话标题——会话存在时返回 True"""
    import json
    from datetime import datetime, timezone

    mock_redis.hget.return_value = json.dumps({
        "title": "旧标题",
        "created_at": datetime.now(timezone.utc).isoformat()
    })

    with patch("app.services.session_service.redis_client", mock_redis):
        result = await session_service.update_session_title(
            user_id=1, session_id="session_1", title="新标题"
        )

        assert result is True
        mock_redis.hset.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_session_title_not_found(mock_redis):
    """测试更新会话标题——会话不存在时返回 False"""
    mock_redis.hget.return_value = None

    with patch("app.services.session_service.redis_client", mock_redis):
        result = await session_service.update_session_title(
            user_id=1, session_id="not_exist", title="新标题"
        )

        assert result is False


@pytest.mark.asyncio
async def test_delete_session(mock_redis):
    """测试删除会话——应同时清理聊天记忆和会话元数据"""
    with patch("app.services.session_service.redis_client", mock_redis):
        result = await session_service.delete_session(
            user_id=1, session_id="session_1"
        )

        assert result is True
        # 验证删除了 List key 和 Hash field
        mock_redis.delete.assert_awaited_once()
        mock_redis.hdel.assert_awaited_once()
