from fastapi.testclient import TestClient

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


def test_chat_endpoint_stores_and_replies():
    """测试咱们刚写好的聊天大闭环：收到能回显且产生记忆"""

    # 我们构造一个假的测试会话
    test_session = "test_auto_001"
    payload = {
        "session_id": test_session,
        "message": "这是一条自动化测试消息"
    }

    # 模拟前端发送 POST 请求到你的核心接口
    response = client.post("/api/v1/chat/", json=payload)

    # 第一步判断：如果连 200 都没返回，说明咱们的格式填错了或者里面代码崩了
    assert response.status_code == 200

    data = response.json()

    # 第二步判断：它是不是乖乖把我的 session_id 还给我了
    assert data["session_id"] == test_session

    # 第三步判断：它的聊天回复里，是不是乖乖包含了我的原话
    assert "这是一条自动化测试消息" in data["reply"]

    # 第四步判断：发送前是0，系统收到1条再模拟回复1条，所以这一回合记完后必须是2条！
    assert data["history_count"] == 2
