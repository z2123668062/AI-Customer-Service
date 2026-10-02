# app/core/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings
from app.core.logging import logger

# SQLite 场景：确保库文件所在目录存在（首次启动自动创建 data/ 等目录）
if settings.DATABASE_URL.startswith("sqlite"):
    from pathlib import Path
    Path(settings.DATABASE_URL.split("///")[-1]).parent.mkdir(parents=True, exist_ok=True)

# 1. 创建全异步的数据库引擎（echo 打印底层 SQL；调试时改 True，评测跑批保持 False 避免日志噪音）
engine = create_async_engine(settings.DATABASE_URL, echo=False)

# 2. 创建异步会话工厂 (就像是给每个请求发一个连接通行证)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# 3. 创建所有数据表模型都要继承的“老祖宗”基类
class Base(AsyncAttrs, DeclarativeBase):
    pass


# 增量列清单（E11 起）：create_all 只建缺表、不给已存在的表加列；后加的列在此登记，
# 启动时幂等补齐（仅 SQLite——本机开发/演示库；MySQL 场景按数据库文档手工 DDL）。
_INCREMENTAL_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "edges": (("cut_at", "DATETIME"),),
}


def _ensure_incremental_columns(sync_conn) -> None:
    """旧库轻量自愈：PRAGMA 查列 → 缺则 ALTER TABLE 补上（幂等，新库零动作）。"""
    if sync_conn.dialect.name != "sqlite":
        return
    for table, columns in _INCREMENTAL_COLUMNS.items():
        existing = {row[1] for row in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # 表尚未存在（异常场景；正常 create_all 已建全列）
        for name, type_name in columns:
            if name not in existing:
                sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {type_name}")
                logger.info(f"库自愈：已为旧表 {table} 补齐增量列 {name}")


async def init_db():
    """建表（幂等）：应用启动时创建 cards / edges / messages 三张表；旧库增量列自愈。"""
    from app.models import domain  # noqa: F401  确保模型已注册到 Base.metadata
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_incremental_columns)