from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from app.models.schemas import RouterResult
# 从你的主文件加载刚刚建好的应用楼
from app.main import app

# 创建一个“虚拟测试员工”，专门负责帮我们自动发请求
client = TestClient(app)


def test_health_check_returns_ok():
    """测试 /health 接口是否能正常返回 status: ok"""

    # 模拟在浏览器老老实实发一个 GET 请求到 /health
    response = client.get("/health")

    # 接下来是自动化测试的核心：“断言”（assert）。
    # 这就是你给机器下的死命令：“如果在这一步你的状态码不是 200，说明接口挂了，立刻给我报错！”
    assert response.status_code == 200

    # 获取返回结果的 JSON 内容
    data = response.json()

    # “断言”这个字典里返回的状态是不是 ok
    assert data["status"] == "ok"


# 【重点修改1】给这个测试方法加上 @patch 装饰器。
# 意思是：拦截掉 app.api.v1.endpoints.chat 里面导入的那个 analyze_intent 函数，
# 把它替换成一个可以自己设返回值的假对象（AsyncMock）。
@patch("app.api.v1.endpoints.chat.analyze_intent", new_callable=AsyncMock)
def test_chat_endpoint_stores_and_replies(mock_analyze_intent):
    """测试咱们修真版（接入路由后）的聊天大闭环"""

    # 【重点修改2】手动给这根“桩”设定一个假的返回死数据。
    # 只要我们的接口测到了那个被拦截的代码，它就不会去联网，直接把这个 RouterResult 按到结果里。
    # 我们假装这次的大模型把它判为 "chitchat"（闲聊）
    mock_analyze_intent.return_value = RouterResult(
        intent="chitchat",
        keywords=["自动化", "测试"]
    )

    test_session = "test_auto_001"
    payload = {
        "session_id": test_session,
        "message": "这是一条自动化测试消息"
    }

    # 模拟前端发送 POST 请求到你的核心接口
    # 测试服务器会自动跑进 chat.py，当它遇到 await analyze_intent(...) 时，就会拿到我们上面捏的面团
    response = client.post("/api/v1/chat/", json=payload)

    # 第一步判断：如果连 200 都没返回，说明代码崩了
    assert response.status_code == 200

    data = response.json()

    # 第二步判断：是不是乖乖把我的 session_id 还给我了
    assert data["session_id"] == test_session

    # 【重点修改3】断言我们修改后的闲聊回复模板
    # 因为路由器被我们锁死返回闲聊，咱们的 chat.py 里写的是：f"系统判定你正在：闲聊。检测到的关键词是：{router_result.keywords}"
    assert "闲聊" in data["reply"]
    assert "测试" in data["reply"]  # 看看刚刚捏在关键字里的词能不能拼出来

    # 第四步判断：发送前是0，系统收到1条再模拟回复1条，所以这一回合记完后必须是2条！
    assert data["history_count"] == 2
