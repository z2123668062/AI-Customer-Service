"""热层（当前对话的 Redis 滑动窗口）+ 组装包。

两件职责：
1. 滑动窗口：历史消息的读写（Redis List）——每轮对话的原文回显来源；
2. 组装包五段（系统设计 §4）：① 系统提示 ② 记忆注入 ③ 任务卡占位
   （V1 恒空）④ 短记忆窗口 ⑤ 当前输入。组装 = 纯函数（本模块）；渲染与发送 =
   生成入口（generation_service.generate_reply）。
"""
import asyncio
import json
from dataclasses import dataclass, field
from typing import List, Literal

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import logger
from app.models.schemas import ChatMessage

redis_client = Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    decode_responses=True,
    # 故障快速降级：Redis 不可达时 1s 内失败，不拖到整轮超时（见 config 注释）
    socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
    socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
    health_check_interval=30,
)
# 窗口长度：容器启动即固定（改 settings.WINDOW_SIZE 或 .env 后重启生效）
SLIDING_WINDOW_SIZE = settings.WINDOW_SIZE

# 系统提示（组装包第①段）：固定内容 = 生成侧实验条件的一部分（换 prompt = 换实验条件）
# L10 修订：原版写着「一定要记住它、一定要结合之前的对话回答哦」——这等于鼓励模型
# 每次回答都把记得的东西倒一遍。记忆应当是隐式上下文，不是展示品。
SYSTEM_PROMPT = (
    "你是天才小羊，一个记性很好的个人助手兼伙伴。"
    "说话直接、短，不绕弯子，不客套开场，不重复用户的话。"
    "会顺着用户的语气和用词，但不讨好、不啰嗦。"
    "一次最多追问一个短问题。"
    "你外接了一套记忆系统：每次回答会拿到最近的历史对话，"
    "以及从用户发言里抽取的记忆卡片作为参考。"
    "不要在回复里暴露你在翻资料，也不要把记得的东西列出来念一遍。"
)


def _key(session_id: str) -> str:
    return f"chat:session:{session_id}"


async def redis_call(awaitable, timeout: float | None = None):
    """所有 Redis 操作的硬超时封装。

    socket_connect_timeout 在 Docker 网络里遇到容器停机时仍可能挂 10s+（连接/DNS
    卡在系统调用），这里再用 asyncio.wait_for 做硬取消：1s 内失败，调用方按降级处理。
    """
    return await asyncio.wait_for(awaitable, timeout=timeout or settings.REDIS_SOCKET_TIMEOUT)


async def add_message(session_id: str, role: Literal["user", "assistant"], content: str) -> ChatMessage:
    """写一条消息进窗口。Redis 故障只降级（窗口缺失），不打断本轮对话。"""
    message = ChatMessage(role=role, content=content)
    redis_key = _key(session_id)
    msg_json = message.model_dump_json()
    try:
        await redis_call(redis_client.rpush(redis_key, msg_json))
        await redis_call(redis_client.ltrim(redis_key, -SLIDING_WINDOW_SIZE, -1))
    except Exception as e:  # noqa: BLE001 —— 热层是加速层，挂了退化为"无窗口"
        logger.warning(f"热层写入失败（会话 {session_id}，窗口降级）: {e}")
    return message


async def get_history(session_id: str) -> List[ChatMessage]:
    """读窗口（最近 SLIDING_WINDOW_SIZE 条）。Redis 故障返回空表——对话退化为单轮，不失败。"""
    redis_key = _key(session_id)
    try:
        raw_messages = await redis_call(redis_client.lrange(redis_key, 0, -1))
    except Exception as e:  # noqa: BLE001 —— 降级是设计的一部分（系统设计 §8）
        logger.warning(f"热层读取失败（会话 {session_id}，退化为单轮）: {e}")
        return []
    history = []
    for raw in raw_messages:
        try:
            history.append(ChatMessage(**json.loads(raw)))
        except Exception as e:  # noqa: BLE001 —— 坏行跳过：窗口是易失层，不值得中断对话
            logger.warning(f"热层消息解析失败（已跳过）: {e}")
    return history


async def get_history_count(session_id: str) -> int:
    redis_key = _key(session_id)
    try:
        return await redis_call(redis_client.llen(redis_key))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"热层计数失败（会话 {session_id}，按 0 处理）: {e}")
        return 0


async def undo_last_turn(session_id: str) -> None:
    """撤销最后一轮对话（用户 + 助手各一条）；Redis 故障静默降级。"""
    redis_key = _key(session_id)
    try:
        count = await redis_call(redis_client.llen(redis_key))
        if count < 2:
            logger.warning(f"会话 {session_id} 中没有足够的消息来撤销。")
            return
        await redis_call(redis_client.rpop(redis_key))
        await redis_call(redis_client.rpop(redis_key))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"热层撤销失败（会话 {session_id}）: {e}")


# ============================================================
# 组装包（E12）：五段结构 + 纯函数组装
# ============================================================

@dataclass
class ContextPackage:
    """组装包五段（发送前形态；渲染为 OpenAI messages 见 generation_service）。

    字段与段位的对应：
    - system_prompt     ① 系统提示（固定人设——实验条件的一部分）
    - memory_injection  ② 记忆注入（有召回就装配；None=本轮不注入）
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

    注入段：有记忆上下文就装配，没有就跳过（"不召回"即本轮不注入，生成行为回落到纯窗口）。
    """
    return ContextPackage(
        system_prompt=SYSTEM_PROMPT,
        current_input=current_input,
        window=list(window or []),
        memory_injection=memory_context or None,
        task_card=None,  # V1 恒空占位（显式字段 + 测试钉死）
    )
