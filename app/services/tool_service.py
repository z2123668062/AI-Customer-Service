import json
from app.core.config import ZHIPU_API_KEY
from openai import OpenAI

# 初始化我们用来做工具调用的客户端
client = OpenAI(
    api_key=ZHIPU_API_KEY,
    base_url="https://open.bigmodel.cn/api/paas/v4/"
)

# ================= 1. 定义本地的老黄牛工具函数 =================

def get_weather(city: str) -> str:
    """模拟查询天气的工具函数"""
    # 真实场景下，这里会去调用外部的高德/和风天气 API
    mock_weather_db = {
        "北京": "晴，气温 15-25度，适合出行。",
        "上海": "多云有阵雨，气温 20-28度，出门记得带伞。",
        "广州": "阴，台风预警，气温 22-30度，尽量减少外出。"
    }
    return mock_weather_db.get(city, f"暂无 {city} 的天气数据，可能是个偏远地区。")

def get_order_status(order_id: str) -> str:
    """模拟查询订单状态的工具函数"""
    # 真实场景下，这里会去连 MySQL 或者调内部的订单微服务
    mock_order_db = {
        "12345": "订单已发货，顺丰快递正在派送中，预计明天上午送达。",
        "67890": "订单正在处理中，库房正在打包。",
        "11111": "订单已取消，退款已按原路返回您的支付账户。"
    }
    # 我们可以做个简单的尾号匹配，显得智能一点
    for key, status in mock_order_db.items():
        if key.endswith(order_id):
            return status
    return f"抱歉，没有找到尾号包含 '{order_id}' 的关联订单。"


# 建立一个函数名映射表，为了等下大模型甩回名字时，代码能知道到底该调哪个
available_functions = {
    "get_weather": get_weather,
    "get_order_status": get_order_status,
}

# ================= 2. 起草给大模型看的“工具说明书” =================
# 这个格式是 OpenAI 制定的业内统一样板 (Function Calling Format)
# 我们把它写得很详细，大模型才会知道在什么情况下调什么函数
tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气情况",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "要查询天气的城市名称，例如 '北京', '上海'"
                    }
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "查询用户的订单物流及处理状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单的编号或末尾几位数字"
                    }
                },
                "required": ["order_id"]
            }
        }
    }
]


# ================= 3. 核心 Agent 调用逻辑 =================

def execute_tool_call(user_input: str) -> str:
    """
    接收用户关于工具调用的意图，自动调度工具并生成人话回复。
    """
    # 第一回合：我们带着用户的原话，和我们手里的“工具库说明书”，去问大模型
    messages = [
        {"role": "system", "content": "你是一个严谨的客服助理，如果用户需要客观记录，请使用提供的工具获取数据后回答。不要废话，直接拿着信息去调工具"},
        {"role": "user", "content": user_input}
    ]

    # 注意这里的 tools=tools_schema，这是告诉大模型：你拥有这些超能力！
    response = client.chat.completions.create(
        model="glm-4-flash",
        messages=messages,
        tools=tools_schema
    )

    # 看看大模型的回答。如果它觉得需要调工具，它会返回一种特殊的状态（tool_calls）
    response_message = response.choices[0].message
    tool_calls = response_message.tool_calls

    # 如果大模型觉得用户的长难句根本不需要调工具（比如用户问天气，但没说城市），它会直接返回普通文字
    if not tool_calls:
        return str(response_message.content)

    # 如果大模型点名要调工具，我们就开始做后端的“工具调用大闭环”
    # 这一步必须要把第一回合大模型的回答，先追加到聊天历史里
    messages.append(response_message)

    # 有可能大模型一口气要求调好几个工具（比如“帮我查北京天气，再查下尾号67890的订单”）
    for tool_call in tool_calls:
        function_name = tool_call.function.name
        # 大模型非常聪明，它已经帮我们把用户话里的参数提取成了 JSON 字符串
        function_args = json.loads(tool_call.function.arguments)

        # 在后端的映射字典里找到那段老黄牛代码
        function_to_call = available_functions.get(function_name)

        if function_to_call:
            # 真实执行我们的本地巨石 Python 代码！(**解包字典为参数)
            print(f"--> [系统底层执行]: 正在调用 {function_name}，参数：{function_args}")
            function_response = function_to_call(**function_args)
        else:
            function_response = f"错误：系统内部找不到名为 {function_name} 的工具接口。"

        # 执行完后，把冰冷的代码运行结果，打包成大模型要求的 tool 身份消息，再发回去
        messages.append(
            {
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": function_name,
                "content": function_response,
            }
        )

    # 第二回合：拿着刚刚执行完的客观数据，再找大模型做最后的“话术包装”
    second_response = client.chat.completions.create(
        model="glm-4-flash",
        messages=messages,
    )

    # 大模型看着冰冷的返回数据（"订单正在处理..."），组织了完美的人话返回给我们
    return f"{second_response.choices[0].message.content}\n\n[来源：外部微服务/API工具聚合]"