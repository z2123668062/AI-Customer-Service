import os

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

# 测试环境兜底：无 .env / 无环境变量时也能导入 Settings（避免测试因密钥缺失直接崩）
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/test.db")


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


# ================================
# 数据层测试夹具（E2 起）：每个测试一个独立临时 SQLite 库
# ================================
@pytest_asyncio.fixture
async def db_session(tmp_path):
    """建好全部表（cards / edges / messages）的独立 AsyncSession。

    每测试一个新库文件（tmp_path），互不污染、跑完即弃；不依赖 .env 与全局 engine——
    store 的会话式 API（supersede_card / rebuild_status 等）直接吃这个 session。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    from app.models import domain  # noqa: F401  注册三表到 Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def db_maker(tmp_path):
    """独立临时库的 async_sessionmaker（E8 起）——供 monkeypatch consolidator.SessionFactory。

    与 db_session 同款建库，但以工厂形式交付（每次调用开新会话）——
    适配自开事务的后台模块（写路径 consolidator / 启动扫描）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    from app.models import domain  # noqa: F401  注册三表到 Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'maker.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# ================================
# 通用假件（E8 起）：Redis List 语义假件（显式缓存测试用）
# ================================
class FakeRedis:
    """真实现 List 语义的 Redis 假件（仅覆盖 explicit 通道用到的子集）。

    不自动替换——由各测试 monkeypatch 到目标模块（如 explicit._redis）。
    """

    def __init__(self):
        self.store: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down (injected)")

    async def rpush(self, key, *values):
        self._check()
        self.store.setdefault(key, []).extend(values)
        return len(self.store[key])

    async def lrange(self, key, start, end):
        self._check()
        items = self.store.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    async def ltrim(self, key, start, end):
        self._check()
        items = self.store.get(key, [])
        self.store[key] = items[start:] if end == -1 else items[start:end + 1]
        return True

    async def expire(self, key, seconds):
        self._check()
        self.expires[key] = seconds
        return True

    async def delete(self, key):
        self._check()
        self.store.pop(key, None)
        self.expires.pop(key, None)
        return 1


@pytest.fixture
def fake_redis():
    """FakeRedis 实例（不自动替换——由各测试 monkeypatch 到目标模块）。"""
    return FakeRedis()
