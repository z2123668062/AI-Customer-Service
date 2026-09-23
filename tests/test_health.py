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
def _parse_sse_events(raw: bytes) -> list[dict]:
    """将 SSE 响应体解析为事件列表"""
    text = raw.decode("utf-8")
    events = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            import json
            events.append(json.loads(line[6:]))
    return events


def _extract_full_reply(events: list[dict]) -> str:
    """从 SSE 事件列表中提取完整的 message 内容"""
    parts = []
    for evt in events:
        if evt.get("event") == "message":
            parts.append(evt["content"])
    return "".join(parts)


@patch("app.api.v1.endpoints.chat.analyze_intent", new_callable=AsyncMock)
@patch("app.api.v1.endpoints.chat.generate_chitchat", new_callable=AsyncMock)
def test_chat_endpoint_stores_and_replies(mock_generate_chitchat, mock_analyze_intent):
    """测试聊天端点：正常置信度走 stream_generator，SSE 流式返回"""

    mock_analyze_intent.return_value = RouterResult(
        intent="chitchat",
        keywords=["自动化", "测试"],
        confidence=1.0,
    )

    mock_generate_chitchat.return_value = "哈哈，测试闲聊回复，包含关键字：测试"

    test_session = "test_auto_001"
    payload = {
        "session_id": test_session,
        "message": "这是一条自动化测试消息"
    }

    response = client.post("/api/v1/chat/", json=payload)

    assert response.status_code == 200

    events = _parse_sse_events(response.content)
    assert len(events) >= 2

    status_event = events[0]
    assert status_event["event"] == "status"

    done_event = events[-1]
    assert done_event["event"] == "done"

    full_reply = _extract_full_reply(events)
    assert "闲聊" in full_reply
    assert "测试" in full_reply
