from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """健康检查接口的返回结构"""

    status: str = Field(default="ok", description="服务状态")


class ChatRequest(BaseModel):
    """用户发给聊天接口的请求结构"""

    session_id: str = Field(..., min_length=1, description="会话ID")
    message: str = Field(..., min_length=1, description="用户输入消息")
    user_id: Optional[str] = Field(default=None, description="用户ID，可选")


class ChatMessage(BaseModel):
    """会话中的单条消息"""

    role: Literal["user", "assistant"] = Field(description="消息角色")
    content: str = Field(..., min_length=1, description="消息内容")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="消息创建时间",
    )


class ChatResponse(BaseModel):
    """聊天接口的返回结构"""

    session_id: str = Field(..., description="会话ID")
    reply: str = Field(..., description="系统回复内容")
    history_count: int = Field(default=0, ge=0, description="当前会话消息数量")
    trace_id: Optional[str] = Field(default=None, description="请求追踪ID，可选")


class RouterResult(BaseModel):
    """
    AI 路由器返回的结构化结果
    """
    # Literal 限制了 intent 只能是这三种字符串中的某一个，类似 Java 的 Enum
    intent: Literal["kb_qa", "chitchat", "tool"] = Field(
        ...,
        description="问题意图。kb_qa: 知识库问答, chitchat: 闲聊, tool: 需要调用工具"
    )

    # 提取的关键字列表，默认给个空列表
    keywords: list[str] = Field(
        default_factory=list,
        description="从用户提问中提取的关键信息，用于后续的知识库检索或工具调用"
    )
