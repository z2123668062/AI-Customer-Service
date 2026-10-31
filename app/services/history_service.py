# app/services/history_service.py
"""消息归档：把一轮对话明文永久写入 messages 表。

"原文永久保留"是硬规定：记忆的摘要压缩（换出）是有损的，原文才是真相（系统设计 §5 硬规定）。
失败只记日志，不影响回复（E12：返回新消息 id 供显式通道暂存——失败返回 None）。
"""
from datetime import datetime

from app.core.database import AsyncSessionLocal
from app.core.logging import logger
from app.models.domain import Message


async def save_record_to_db(session_id: str, role: str, content: str,
                           created_at: datetime | None = None) -> int | None:
    """把一条聊天记录写入 messages 归档表，返回自增 id（失败 None——调用方降级处理）。

    created_at：消息**发生时间**（L3 定稿，2026-10-30）。不传 = 数据库 server_default（当前时刻），
    线上对话路径就是这种情况（此刻说的就是此刻发生的，两者等价）。
    历史数据回放（基准复现）必须显式传入真实发生时间——否则抽取器会把"2023 年发生的事"
    按入库日期换算，event_time 全错。
    """
    try:
        # async with 自动开启会话并在结束时安全归还连接池
        async with AsyncSessionLocal() as db:
            message = Message(session_id=session_id, role=role, content=content)
            if created_at is not None:
                message.created_at = created_at
            db.add(message)
            await db.flush()          # 拿到自增 id（commit 前）
            message_id = message.id
            await db.commit()
            return message_id
    except Exception as e:
        logger.error(f"存入归档失败! 会话:{session_id}, 错误:{e}")
        return None
