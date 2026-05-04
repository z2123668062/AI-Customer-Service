import pytest
from unittest.mock import patch, MagicMock
from app.services.tool_service import get_weather, get_order_status, execute_tool_call

def test_get_weather():
    """测试原生本地工具：查天气"""
    assert "晴" in get_weather("北京")
    assert "暂无" in get_weather("火星")

def test_get_order_status():
    """测试原生本地工具：查订单"""
    assert "顺丰快递" in get_order_status("12345")
    assert "抱歉" in get_order_status("99999")

@patch("app.services.tool_service.client.chat.completions.create")
def test_execute_tool_call(mock_create):
    """
    测试高级特工(Agent)逻辑：模拟大模型分析得出 Tool_Call，
    然后验证本地执行是否闭环，再传回给大模型说话。
    """
    # 第一阶段：模拟大模型的第一回合返回。它决定要调工具。
    mock_tool_call = MagicMock()
    mock_tool_call.function.name = "get_order_status"
    mock_tool_call.function.arguments = '{"order_id": "12345"}'
    mock_tool_call.id = "call_test_123"

    mock_message_1 = MagicMock()
    mock_message_1.tool_calls = [mock_tool_call]
    mock_message_1.content = None

    mock_response_1 = MagicMock()
    mock_response_1.choices = [MagicMock(message=mock_message_1)]

    # 第二阶段：我们预期本地真函数执行后，再次带着结果去问大模型。
    # 模拟大模型的第二回合润色回答。
    mock_message_2 = MagicMock()
    mock_message_2.tool_calls = None
    mock_message_2.content = "您的订单12345已发货，由顺丰快递派送。"

    mock_response_2 = MagicMock()
    mock_response_2.choices = [MagicMock(message=mock_message_2)]

    # 重点：side_effect 传递一个列表，表示只要它调用了 create 方法：
    # 第一次调用返回 mock_response_1，第二次调用返回 mock_response_2。
    mock_create.side_effect = [mock_response_1, mock_response_2]

    # 执行业务逻辑
    result = execute_tool_call("查一下订单12345")

    # 执行断言
    assert "顺丰快递" in result
    assert "[来源：外部微服务/API工具聚合]" in result

    # 验证是否严谨地与大模型往返通讯了整整2个回合！
    assert mock_create.call_count == 2

