import pytest
from unittest.mock import patch
from app.services import ratelimit_service


@pytest.mark.asyncio
async def test_check_chat_limit_within(mock_redis):
    """测试聊天限流——未超限时应返回 True"""
    mock_redis.incr.return_value = 5  # 第 5 次请求，未超限（上限 20）

    with patch("app.services.ratelimit_service.redis_client", mock_redis):
        result = await ratelimit_service.check_chat_limit(user_id=1)

        assert result is True
        mock_redis.incr.assert_awaited_once()
        # count != 1，不应设置过期
        mock_redis.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_chat_limit_exceeded(mock_redis):
    """测试聊天限流——超限时应返回 False"""
    mock_redis.incr.return_value = 21  # 第 21 次请求，超限（上限 20）

    with patch("app.services.ratelimit_service.redis_client", mock_redis):
        result = await ratelimit_service.check_chat_limit(user_id=1)

        assert result is False


@pytest.mark.asyncio
async def test_check_chat_limit_first_request(mock_redis):
    """测试聊天限流——第一次请求时应设置过期"""
    mock_redis.incr.return_value = 1  # 第一次请求

    with patch("app.services.ratelimit_service.redis_client", mock_redis):
        result = await ratelimit_service.check_chat_limit(user_id=1)

        assert result is True
        mock_redis.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_session_limit_within(mock_redis):
    """测试会话限流——未超限时应返回 True"""
    mock_redis.incr.return_value = 3

    with patch("app.services.ratelimit_service.redis_client", mock_redis):
        result = await ratelimit_service.check_session_limit(user_id=1)

        assert result is True


@pytest.mark.asyncio
async def test_check_session_limit_exceeded(mock_redis):
    """测试会话限流——超限时应返回 False"""
    mock_redis.incr.return_value = 6  # 第 6 次请求，超限（上限 5）

    with patch("app.services.ratelimit_service.redis_client", mock_redis):
        result = await ratelimit_service.check_session_limit(user_id=1)

        assert result is False
