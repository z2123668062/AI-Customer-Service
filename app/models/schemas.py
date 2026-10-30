from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthResponse(BaseModel):
    status: str = Field(default="ok", description="服务状态")


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


# ================================
# 记忆管理接口（api/v1/endpoints/memory.py）的请求/响应模型（E11）
# ================================

class CardItem(BaseModel):
    """记忆卡列表项（轻量快照；边与版本链见 CardDetail）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str = Field(description="entity / event")
    subtype: Optional[str] = Field(default=None, description="entity 子类：identity/preference/goal/relation")
    content: str = Field(description="卡片正文")
    entities: list[str] = Field(default_factory=list, description="关联实体名")
    event_time: Optional[datetime] = Field(default=None, description="事件/目标时间")
    source: str = Field(description="passive / explicit")
    status: str = Field(description="active / superseded / revoked / evicted")
    importance: Optional[float] = Field(default=None)
    source_session_id: Optional[str] = Field(default=None, description="来源会话（溯源可跳回）")
    access_count: int = Field(default=0, description="被检索访问次数")
    last_accessed_at: Optional[datetime] = Field(default=None)
    created_at: Optional[datetime] = Field(default=None)

    @field_validator("entities", mode="before")
    @classmethod
    def _entities_none_to_empty(cls, v):
        """DB 里 entities 可为 NULL——响应口径统一为数组。"""
        return v or []


class CardListResponse(BaseModel):
    """列表响应：asc 模式游标分页；next_after_id=None 表示无更多。"""

    items: list[CardItem] = Field(default_factory=list)
    count: int = Field(default=0, description="本页条数")
    next_after_id: Optional[int] = Field(default=None, description="下一批起点（仅 asc 模式）")


class EdgeItem(BaseModel):
    """边（线）条目：剪断的边也返回（cut_at 非空）——面板审计视图。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    from_card_id: int
    to_card_id: int
    kind: str = Field(description="caused_by / led_to / supersedes")
    source_session_id: Optional[str] = Field(default=None)
    cut_at: Optional[datetime] = Field(default=None, description="剪断时间；NULL=未剪断")
    created_at: Optional[datetime] = Field(default=None)


class CardDetail(CardItem):
    """单卡详情：卡 + 双向相关边（含剪断标记）+ 版本历史链。"""

    edges: list[EdgeItem] = Field(default_factory=list, description="双向相关边（含已剪断）")
    superseded_by: Optional[int] = Field(default=None, description="当前取代者（未剪断入边来源）；None=无")
    versions: list[CardItem] = Field(default_factory=list, description="版本历史（本卡取代过的旧版本，近→远）")


class CardUpdateRequest(BaseModel):
    """PATCH 内容修订（不原地改：新卡 + supersedes 旧卡）。"""

    content: str = Field(..., min_length=1, max_length=2000, description="修订后的正文")


class EditResponse(BaseModel):
    """编辑层操作统一回执。"""

    ok: bool = Field(default=True)
    action: str = Field(description="edit / delete / restore / rollback / purge / cut_edge")
    card_id: Optional[int] = Field(default=None)
    new_card_id: Optional[int] = Field(default=None, description="edit：新版本卡 id")
    affected_card_ids: list[int] = Field(default_factory=list, description="rollback/purge：连带变更的卡")
    cut_edge_ids: list[int] = Field(default_factory=list, description="rollback/cut：剪断的边")
    detail: Optional[str] = Field(default=None)


class MemoryStats(BaseModel):
    """总览统计（面板 sidebar / 首页卡片）。"""

    total: int = Field(default=0, description="卡片总数（全状态）")
    by_status: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)
    pool: dict[str, int] = Field(default_factory=dict, description="快表观测（capacity/cards/…）")
    last_evict_ts: Optional[str] = Field(default=None, description="最近一次淘汰巡检时间")


class EvictRunResponse(BaseModel):
    """淘汰巡检（sweep）报告。"""

    scanned: int = Field(default=0, description="扫描的 active 卡数")
    evicted: int = Field(default=0, description="本次下架数")
    details: list[dict] = Field(default_factory=list, description="每卡下架理由")
    stage_ms: dict[str, float] = Field(default_factory=dict, description="各阶段耗时")


class EvictLogResponse(BaseModel):
    """淘汰日志（最新在前）。"""

    items: list[dict] = Field(default_factory=list)
