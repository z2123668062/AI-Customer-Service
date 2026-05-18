# app/models/domain.py
from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from app.core.database import Base

class ChatRecord(Base):
    # 这就是真正在 MySQL 里叫的名字
    __tablename__ = "chat_records"

    # 主键自增
    id = Column(Integer, primary_key=True, index=True)
    # 给 session_id 加索引 (index=True)，因为我们总要“按 session_id 查历史”
    session_id = Column(String(255), index=True, comment="会话标识")
    role = Column(String(50), comment="用户还是AI")
    content = Column(Text, comment="聊天内容")
    # 让数据库自动打上时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())