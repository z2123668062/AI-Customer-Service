import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from app.services import auth_service
from app.models.domain import User


@pytest.mark.asyncio
async def test_register_user_success():
    """测试注册新用户——数据库返回 None（用户名不存在），应成功创建用户"""
    with patch("app.services.auth_service.AsyncSessionLocal") as mock_session_factory:
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__.return_value = mock_session

        # 用 MagicMock 而非 AsyncMock——scalar_one_or_none 是同步方法
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        user = await auth_service.register_user("new_user", "password123")

        assert user.username == "new_user"
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_user_duplicate():
    """测试注册已存在的用户名——应抛出 ValueError"""
    with patch("app.services.auth_service.AsyncSessionLocal") as mock_session_factory:
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__.return_value = mock_session

        # 模拟已存在的用户
        existing_user = User(username="existing", password_hash="hash")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_session.execute.return_value = mock_result

        with pytest.raises(ValueError, match="用户名已存在"):
            await auth_service.register_user("existing", "password123")


@pytest.mark.asyncio
async def test_login_user_success():
    """测试登录——正确的用户名和密码应返回用户"""
    with patch("app.services.auth_service.AsyncSessionLocal") as mock_session_factory:
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__.return_value = mock_session

        password_hash = auth_service.hash_password("correct_password")
        user = User(username="test_user", password_hash=password_hash)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_session.execute.return_value = mock_result

        result = await auth_service.login_user("test_user", "correct_password")
        assert result.username == "test_user"


@pytest.mark.asyncio
async def test_login_user_wrong_password():
    """测试登录——错误密码应抛出 ValueError"""
    with patch("app.services.auth_service.AsyncSessionLocal") as mock_session_factory:
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__.return_value = mock_session

        password_hash = auth_service.hash_password("correct_password")
        user = User(username="test_user", password_hash=password_hash)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_session.execute.return_value = mock_result

        with pytest.raises(ValueError, match="用户名或密码错误"):
            await auth_service.login_user("test_user", "wrong_password")


def test_create_and_decode_token():
    """测试 JWT Token 的创建和解码"""
    token = auth_service.create_token(user_id=1, username="test_user")
    assert token is not None

    payload = auth_service.decode_token(token)
    assert payload is not None
    assert payload["user_id"] == 1
    assert payload["username"] == "test_user"


def test_sms_code_flow():
    """测试发送和验证短信验证码的完整流程。

    注意：send_sms_code 和 verify_sms_code 是同步函数，调用了异步 Redis 方法。
    由于它们没有 await Redis 调用，mock 需要用 MagicMock（返回值不会被包装成 coroutine）。
    """
    mock_redis = MagicMock()
    mock_redis.setex = MagicMock(return_value=True)

    with patch("app.services.auth_service.redis_client", mock_redis):
        code = auth_service.send_sms_code("13800138000")
        assert len(code) == 6
        assert code.isdigit()

        # 验证码正确——get 返回正确的验证码
        mock_redis.get = MagicMock(return_value=code)
        mock_redis.delete = MagicMock(return_value=True)

        result = auth_service.verify_sms_code("13800138000", code)
        assert result is True

        # 验证码错误
        mock_redis.get = MagicMock(return_value=code)
        result = auth_service.verify_sms_code("13800138000", "000000")
        assert result is False
