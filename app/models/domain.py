# app/models/domain.py
from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from app.core.database import Base

class ChatRecord(Base):
    # 这就是真正在 MySQL 里叫的名字
    __tablename__ = "chat_records"

    # 主键自增
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=True, comment="用户ID")
    session_id = Column(String(255), index=True, comment="会话标识")
    role = Column(String(50), comment="用户还是AI")
    content = Column(Text, comment="聊天内容")
    # 让数据库自动打上时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, index=True, nullable=False, comment="用户名")
    password_hash = Column(String(255), nullable=False, comment="密码哈希值")
    phone = Column(String(20), nullable=True, index=True, comment="手机号")
    created_at = Column(DateTime(timezone=True), server_default=func.now())