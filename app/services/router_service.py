import json
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from app.models.schemas import RouterResult
from app.core.config import ZHIPU_API_KEY

client = AsyncOpenAI(
    api_key=ZHIPU_API_KEY,
    base_url="https://open.bigmodel.cn/api/paas/v4/"
)


async def analyze_intent(user_input: str) -> RouterResult:
    """
    语义路由器：根据用户的输入，分析意图提取关键字
    """

    system_prompt = """
    你是一个智能网络请求路由器。
    你的任务是分析用户的输入，判断意图并提取核心关键字。

    意图只能是以下三种之一：
    - kb_qa: 用户在询问专业知识、公司政策、操作指南等，需要查阅知识库。
    - tool: 用户要求执行某个动作（如查天气、发邮件、建日程、查数据库）。
    - chitchat: 用户在日常打招呼、闲聊、表达情绪等。

    你必须且只能返回纯 JSON 格式数据，不能包含任何多余的废话和 markdown 标记！
    返回数据结构的示范如下：
    {
        "intent": "chitchat",
        "keywords": ["你好"]
    }
    """

    # 加上类型声明消除 IDE 警告
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"用户输入内容如下:\n{user_input}"}
    ]

    response = await client.chat.completions.create(
        model="glm-4-flash",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.0
    )

    ai_result_str = response.choices[0].message.content
    result_dict = json.loads(ai_result_str)

    return RouterResult(**result_dict)