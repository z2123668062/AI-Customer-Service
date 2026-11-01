# app/api/v1/endpoints/sessions.py
"""会话列表接口（L11）：会话是**话题容器**，不是记忆边界。

设计口径（写进面板文案，避免误解）：
- 记忆全局共享：`cards` 表不按会话分，切会话不会丢记忆；
- 会话只决定「哪 20 条原文进短期窗口」；
- 所以这个列表的唯一用途是**切回历史对话**，不是隔离用户。

端点：
- GET /sessions                     会话列表（标题取首条用户消息，按最后活跃倒序）
- GET /sessions/{session_id}/messages  某会话的完整消息（切回时回放）
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.domain import Message

router = APIRouter()


async def get_session():
    """请求级数据库会话（只读端点，无需提交）。"""
    async with AsyncSessionLocal() as session:
        yield session


class SessionSummary(BaseModel):
    session_id: str
    title: str = Field(description="首条用户消息截断；没有用户消息时为「（空会话）」")
    messages: int = Field(description="消息条数")
    started_at: Optional[datetime] = None
    last_at: Optional[datetime] = None


class SessionListResponse(BaseModel):
    items: list[SessionSummary] = Field(default_factory=list)
    count: int = 0


class MessageItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: Optional[datetime] = None


class SessionMessages(BaseModel):
    session_id: str
    messages: list[MessageItem] = Field(default_factory=list)


@router.get("", response_model=SessionListResponse)
async def list_sessions(limit: int = Query(default=50, ge=1, le=200),
                        session: AsyncSession = Depends(get_session)):
    """会话列表：按最后活跃倒序。标题取该会话第一条用户消息。"""
    rows = (await session.execute(
        select(Message.session_id,
               func.count(Message.id),
               func.min(Message.created_at),
               func.max(Message.created_at))
        .where(Message.session_id.is_not(None))
        .group_by(Message.session_id)
        .order_by(func.max(Message.created_at).desc())
        .limit(limit)
    )).all()
    if not rows:
        return SessionListResponse(items=[], count=0)

    # 标题：每个会话的第一条用户消息（一次查完，避免 N+1）
    first_rows = (await session.execute(
        select(Message.session_id, func.min(Message.id))
        .where(Message.session_id.is_not(None), Message.role == "user")
        .group_by(Message.session_id)
    )).all()
    titles: dict[str, str] = {}
    first_ids = [mid for _, mid in first_rows]
    if first_ids:
        msgs = (await session.execute(
            select(Message).where(Message.id.in_(first_ids))
        )).scalars().all()
        for m in msgs:
            text = (m.content or "").strip().replace("\n", " ")
            titles[m.session_id] = (text[:28] + "…") if len(text) > 28 else text

    items = [
        SessionSummary(session_id=sid, title=titles.get(sid) or "（空会话）",
                       messages=cnt, started_at=start, last_at=last)
        for sid, cnt, start, last in rows
    ]
    return SessionListResponse(items=items, count=len(items))


@router.get("/{session_id}/messages", response_model=SessionMessages)
async def session_messages(session_id: str,
                           limit: int = Query(default=500, ge=1, le=2000),
                           session: AsyncSession = Depends(get_session)):
    """某会话的完整消息（升序），用于切回历史会话时回放。"""
    msgs = (await session.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.id.asc())
        .limit(limit)
    )).scalars().all()
    return SessionMessages(session_id=session_id,
                           messages=[MessageItem.model_validate(m) for m in msgs])
