"""统一生成器：全系统唯一的 LLM 生成入口（架构硬约束 6）。

E12 起接收组装包（short_term_memory.ContextPackage 五段）——本模块只负责渲染
messages、发送与容错；prompt 内容归组装包（固定模型 / 固定 prompt / 固定温度，
换任一 = 换实验条件）。

约束：
- 固定模型、固定 prompt、固定温度，保证评测生成侧变量一致；
- 返回 GenerationResult（含 token 用量——埋点 cost.tokens 的数据源）。
"""
from dataclasses import dataclass

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import settings
from app.core.logging import logger
from app.core.short_term_memory import ContextPackage

client = AsyncOpenAI(
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL
)

# 兜底话术：不暴露内部异常（旧实现把 str(e) 拼进回复，既泄露给用户、又被当作
# 助手发言归档）。评测脚本按「走神」标记识别失败行，文案保留该词。
_FALLBACK_REPLY = "不好意思，我刚才走神了，你能再说一遍吗？"


@dataclass
class GenerationResult:
    """一轮生成的完整产出：回复文本 + token 用量（端点不返回 usage 时为空）。"""

    reply: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


def render_messages(package: ContextPackage) -> list[ChatCompletionMessageParam]:
    """组装包五段 → OpenAI messages（拼接纪律的唯一落点）。

    顺序：① system 提示 → ② 注入（附加 system 段，空则跳过）→ ③ 任务卡（V1 恒空
    跳过；机制启用时在此渲染）→ ④ 窗口逐条 → ⑤ 当前输入（末尾 user 消息）。
    只放非空文字（空消息在兼容端点上易触发请求报错）。
    """
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": package.system_prompt}
    ]
    if package.memory_injection:
        messages.append({"role": "system", "content": package.memory_injection})
    if package.task_card:
        messages.append({"role": "system", "content": package.task_card})
    for msg in package.window:
        if msg.content.strip():
            messages.append({"role": msg.role, "content": msg.content})
    if package.current_input.strip():
        messages.append({"role": "user", "content": package.current_input})
    return messages


async def generate_reply(package: ContextPackage) -> GenerationResult:
    """根据组装包生成回复（异常兜底文案，保证返回非空）。"""
    messages = render_messages(package)
    try:
        response = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=0.7
        )
        usage = response.usage
        reply = (response.choices[0].message.content or "").strip()
        if not reply:
            # 空回复会让 ChatMessage(content min_length=1) 校验失败，连带整轮被丢弃
            logger.warning("[generation] 模型返回空内容，已替换为兜底话术")
            reply = _FALLBACK_REPLY
        return GenerationResult(
            reply=reply,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )
    except Exception as e:  # noqa: BLE001 —— 生成失败由兜底话术保底，不让整轮 500
        logger.error(f"[generation] 生成失败: {type(e).__name__}: {e}")
        return GenerationResult(reply=_FALLBACK_REPLY)
