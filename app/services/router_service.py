import json
from openai import AsyncOpenAI
# 导入我们刚刚定义的数据结构
from app.models.schemas import RouterResult

# 1. 初始化 OpenAI 客户端，但指向智谱的服务器
# 就像 JDBC 换了 MySQL 驱动连 Oracle 一样，底层协议通用的
client = AsyncOpenAI(
    api_key="446b72814b554a46967c4df4563567fb.Rta5DU3vQlYBC7hF",
    base_url="https://open.bigmodel.cn/api/paas/v4/"  # 智谱的 OpenAI 兼容接口地址
)


async def analyze_intent(user_input: str) -> RouterResult:
    """
    语义路由器：根据用户的输入，分析意图提取关键字
    """

    # 2. 给出主考官指令（System Prompt）
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
    # 3. 调用智谱 GLM-4-Flash 模型
    response = await client.chat.completions.create(
        model="glm-4-flash",  # 指定使用智谱免费的 Flash 模型
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"用户输入内容如下:\n{user_input}"}
        ],
        response_format={"type": "json_object"},  # 强制AI返回JSON
        temperature=0.0  # temperature=0.0 表示让它的输出尽可能固定，不要瞎发挥
    )

    # 4. 拿到 AI 返回的字符串数据
    ai_result_str = response.choices[0].message.content

    # 5. 把字符串强转为 Python 里面的字典
    result_dict = json.loads(ai_result_str)

    # 6. 用定义的 Pydantic 数据结构进行封装校验
    return RouterResult(**result_dict)