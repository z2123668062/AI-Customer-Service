import pytest
from unittest.mock import AsyncMock, MagicMock


# ================================
# 全局 pytest-asyncio 配置
# ================================
# 所有以 test_ 开头的异步测试函数都会自动被 pytest-asyncio 接管
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_redis():
    """统一的 Redis mock fixture——所有测试文件都能用。

    覆盖 redis.asyncio.Redis 的常用方法：
    - 键值操作: get / setex / delete / incr / expire
    - List 操作: rpush / lrange / llen / ltrim / rpop
    - Hash 操作: hset / hget / hgetall / hdel
    - 分布式锁: lock().acquire() / lock().release()
    """
    mock = AsyncMock()

    # 计数类
    mock.incr = AsyncMock(return_value=1)
    mock.expire = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=True)

    # List 操作
    mock.rpush = AsyncMock(return_value=1)
    mock.lrange = AsyncMock(return_value=[])
    mock.llen = AsyncMock(return_value=0)
    mock.ltrim = AsyncMock(return_value=True)
    mock.rpop = AsyncMock(return_value=None)

    # Hash 操作
    mock.hset = AsyncMock(return_value=True)
    mock.hgetall = AsyncMock(return_value={})
    mock.hget = AsyncMock(return_value=None)
    mock.hdel = AsyncMock(return_value=1)

    # KV 操作
    mock.setex = AsyncMock(return_value=True)
    mock.get = AsyncMock(return_value=None)

    # 分布式锁
    mock_lock = MagicMock()
    mock_lock.acquire = AsyncMock(return_value=True)
    mock_lock.release = AsyncMock(return_value=True)
    mock.lock = MagicMock(return_value=mock_lock)

    return mock
