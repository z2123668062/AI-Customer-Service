from openai import AsyncOpenAI
from app.core.config import settings
from app.models.schemas import RouterResult

client = AsyncOpenAI(
    api_key=settings.ZHIPU_API_KEY,
    base_url="https://open.bigmodel.cn/api/paas/v4/"
)

CLARIFICATION_CONFIDENCE_THRESHOLD = settings.CLARIFICATION_CONFIDENCE_THRESHOLD


async def generate_clarification(user_input: str, router_result: RouterResult) -> str:
    system_prompt = f"""
你是一个 AI 客服助手的"意图确认员"。你的任务是在系统不确定用户意图时，向用户提出一个自然、友好的反问以确认。

系统初步分析的结果：
- 意图：{router_result.intent}
- 关键词：{', '.join(router_result.keywords)}
- 置信度：{router_result.confidence}

请基于以上分析，用一句话反问用户，确认他是否真的想做这件事。
要求：语气自然友好，直接说出你的理解来问用户对不对。不要复述"意图"、"关键词"、"置信度"这些术语。
注意：只输出反问内容，不要输出任何额外信息。
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"用户说：{user_input}"}
    ]

    try:
        response = await client.chat.completions.create(
            model="glm-4-flash",
            messages=messages,
            temperature=0.7,
        )
        return response.choices[0].message.content
    except Exception:
        return "抱歉，我没太理解您的意思。您是想查询相关信息吗？请说得更具体一些。"