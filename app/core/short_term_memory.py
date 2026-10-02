"""热层（当前对话的 Redis 滑动窗口，同时是 B2 baseline 档位）+ 组装包（E12）。

两件职责：
1. 滑动窗口：历史消息的读写（Redis List）——B2 基线通路，永不删除；
2. 组装包五段（系统设计 §4，E12 落地）：① 系统提示 ② 记忆注入 ③ 任务卡占位
   （V1 恒空）④ 短记忆窗口 ⑤ 当前输入。组装 = 纯函数（本模块）；渲染与发送 =
   生成入口（generation_service.generate_reply）。

CONTEXT_MODE 分档（素材准备归调用方 chat_pipeline，口径见《联调收口文档.md》）：
- full_history：窗口段 = messages 表最近 FULL_HISTORY_MAX_MESSAGES 条（B1）；
- window / window+memory：窗口段 = 本模块热层窗口（B2~B5）；注入段仅后者装配。
"""
import json
from dataclasses import dataclass, field
from typing import List, Literal

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import logger
from app.models.schemas import ChatMessage

redis_client = Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB, decode_responses=True)
# 窗口消息数收归开关矩阵（E3）：在此改 = 改 settings.WINDOW_SIZE（.env 可覆盖）
SLIDING_WINDOW_SIZE = settings.WINDOW_SIZE

# 系统提示（组装包第①段）：固定内容 = 生成侧实验条件的一部分（换 prompt = 换实验条件）
SYSTEM_PROMPT = (
    "你是一个热情、有同理心、幽默的个人助手。"
    "如果有用户告诉你名字，一定要记住它。如果有连续对话，一定要结合之前的对话回答哦。"
)


def _key(session_id: str) -> str:
    return f"chat:session:{session_id}"


async def add_message(session_id: str, role: Literal["user", "assistant"], content: str) -> ChatMessage:
    message = ChatMessage(role=role, content=content)
    redis_key = _key(session_id)
    msg_json = message.model_dump_json()
    await redis_client.rpush(redis_key, msg_json)
    await redis_client.ltrim(redis_key, -SLIDING_WINDOW_SIZE, -1)
    return message


async def get_history(session_id: str) -> List[ChatMessage]:
    redis_key = _key(session_id)
    raw_messages = await redis_client.lrange(redis_key, 0, -1)
    history = []
    for raw in raw_messages:
        data = json.loads(raw)
        history.append(ChatMessage(**data))
    return history


async def get_history_count(session_id: str) -> int:
    redis_key = _key(session_id)
    return await redis_client.llen(redis_key)


async def undo_last_turn(session_id: str) -> None:
    """撤销最后一轮对话（用户 + 助手各一条）"""
    redis_key = _key(session_id)
    count = await redis_client.llen(redis_key)
    if count < 2:
        logger.warning(f"会话 {session_id} 中没有足够的消息来撤销。")
        return
    await redis_client.rpop(redis_key)
    await redis_client.rpop(redis_key)


# ============================================================
# 组装包（E12）：五段结构 + 纯函数组装
# ============================================================

@dataclass
class ContextPackage:
    """组装包五段（发送前形态；渲染为 OpenAI messages 见 generation_service）。

    字段与段位的对应：
    - system_prompt     ① 系统提示（固定人设——实验条件的一部分）
    - memory_injection  ② 记忆注入（仅 CONTEXT_MODE=window+memory 档装配；None=无注入段）
    - task_card         ③ 任务卡占位（V1 恒空——任务卡机制后置，系统设计 §7）
    - window            ④ 短记忆窗口（full_history / 热层窗口；素材由调用方按档准备）
    - current_input     ⑤ 当前输入（本轮用户消息——不再混入窗口段，独立成段）
    """

    system_prompt: str
    current_input: str
    window: List[ChatMessage] = field(default_factory=list)
    memory_injection: str | None = None
    task_card: str | None = None


def assemble_package(
    *,
    current_input: str,
    window: List[ChatMessage] | None = None,
    memory_context: str | None = None,
) -> ContextPackage:
    """组装包纯函数（五段；无 IO、无 LLM——素材全部由调用方准备）。

    注入装配口径（旋钮正交）：仅 `CONTEXT_MODE == "window+memory"` 档装配注入段；
    `window` / `full_history` 档即使传入 memory_context 也不装配（消融的干净边界）。
    """
    injection = (
        memory_context
        if settings.CONTEXT_MODE == "window+memory" and memory_context
        else None
    )
    return ContextPackage(
        system_prompt=SYSTEM_PROMPT,
        current_input=current_input,
        window=list(window or []),
        memory_injection=injection,
        task_card=None,  # V1 恒空占位（显式字段 + 测试钉死）
    )
