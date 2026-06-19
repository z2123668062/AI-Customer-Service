# app/services/history_service.py
from app.core.database import AsyncSessionLocal
from app.models.domain import ChatRecord
from app.core.logging import logger


async def save_record_to_db(session_id: str, role: str, content: str, user_id: int = None):
    """甩手掌柜任务：把一条聊天记录存入 MySQL 归档"""
    try:
        # async with 会自动帮我们开启会话，并且在结束时安全关闭（归还连接池）
        async with AsyncSessionLocal() as db:
            # 1. 实例化我们的 ORM 模型
            new_record = ChatRecord(
                session_id=session_id,
                role=role,
                content=content,
                user_id=user_id
            )
            # 2. 扔进会话
            db.add(new_record)
            # 3. 提交事务，真正执行 INSERT SQL 语句
            await db.commit()

    except Exception as e:
        logger.error(f"存入 MySQL 归档失败! 会话:{session_id}, 错误:{e}")