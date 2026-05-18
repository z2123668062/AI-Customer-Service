# app/core/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

# 1. 创建全异步的数据库引擎 (echo=True 可以在控制台打印底层生成的原生 SQL 语句，方便我们学习)
engine = create_async_engine(settings.DATABASE_URL, echo=True)

# 2. 创建异步会话工厂 (就像是给每个请求发一个连接通行证)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# 3. 创建所有数据表模型都要继承的“老祖宗”基类
class Base(AsyncAttrs, DeclarativeBase):
    pass