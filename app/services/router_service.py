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
    你是一个极其严格的网络请求路由器。你必须阅读用户的输入，并输出符合下面 JSON 结构的分析结果。

    你的 JSON 必须严格包含 "intent" 和 "keywords" 两个字段。
    
    "intent" 字段的取值规范极其严格，只能是以下四种的其中之一：
    - "tool": 当用户的话语中出现【查天气、看订单、退款进度、发邮件、创建日程】等需要你系统去执行查询和操作动作时。
    - "kb_qa": 当用户询问公司的各种【死板规定、制度、流程】需要翻书时。
    - "complaint": 当用户的表达中充满【愤怒、指责、投诉意图】，或者明确要求【见真人、转人工、经理出来】时。
    - "chitchat": 当用户进行与具体业务毫无关联的【打招呼】，情感平淡且不符合上述几条时。

    【示例】
    用户：你好客服，帮忙查个急件订单，尾号是 67890 那个到哪儿了
    输出：{"intent": "tool", "keywords": ["67890", "急件", "查订单"]}
    用户：你们这什么破公司，这么久还不发货！叫你们经理出来！
    输出：{"intent": "complaint", "keywords": ["不发货", "转人工", "投诉"]}
    用户：你们公司出差怎么报销？
    输出：{"intent": "kb_qa", "keywords": ["出差", "报销"]}

    请严格只返回合格的 JSON，不能有任何说明！
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


# app/services/router_service.py 底部增加：

async def generate_chitchat(user_input: str) -> str:
    """真正的闲聊生成器"""
    # 解决不够生动、太简短的关键：Prompt System 人设设定
    system_prompt = (
        "你是一个热情、有同理心、幽默的 AI 客服助手。"
        "哪怕用户只是简单的打招呼，你也应该像老朋友一样给予温暖且字数丰富生动的回应。"
    )

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]

    try:
        response = await client.chat.completions.create(
            model="glm-4-flash",
            messages=messages,
            temperature=0.7  # 0表示严谨死板，0.7表示有一定的自由发散创造力
        )
        return response.choices[0].message.content
    except Exception as e:
        return "不好意思，我刚才走神了，你能再说一遍吗？"