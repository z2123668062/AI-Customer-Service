from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(default="ok", description="服务状态")


class UserRegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50, description="用户名")
    password: str = Field(..., min_length=6, max_length=100, description="密码")
    phone: Optional[str] = Field(default=None, description="手机号，可选")


class UserLoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class UserResponse(BaseModel):
    id: int
    username: str
    phone: Optional[str] = None
    created_at: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT Token")
    token_type: str = Field(default="bearer", description="Token 类型")
    user: UserResponse = Field(..., description="用户信息")


class SendCodeRequest(BaseModel):
    phone: str = Field(..., pattern=r"^1\d{10}$", description="手机号，11位数字")


class PhoneLoginRequest(BaseModel):
    phone: str = Field(..., pattern=r"^1\d{10}$", description="手机号")
    code: str = Field(..., min_length=4, max_length=6, description="验证码")


class SessionCreateRequest(BaseModel):
    title: Optional[str] = Field(default="新对话", max_length=100, description="会话标题")


class SessionUpdateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100, description="会话标题")


class SessionResponse(BaseModel):
    session_id: str
    title: str
    created_at: Optional[str] = None


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="会话ID")
    message: str = Field(..., min_length=1, description="用户输入消息")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = Field(description="消息角色")
    content: str = Field(..., min_length=1, description="消息内容")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="消息创建时间",
    )


class ChatResponse(BaseModel):
    session_id: str = Field(..., description="会话ID")
    reply: str = Field(..., description="系统回复内容")
    history_count: int = Field(default=0, ge=0, description="当前会话消息数量")
    trace_id: Optional[str] = Field(default=None, description="请求追踪ID，可选")


class RouterResult(BaseModel):
    intent: Literal["kb_qa", "chitchat", "tool", "complaint"] = Field(
        ...,
        description="问题意图。kb_qa: 知识库问答, chitchat: 闲聊, tool: 需要调用工具, complaint: 投诉或强烈不满"
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="从用户提问中提取的关键信息，用于后续的知识库检索或工具调用"
    )
