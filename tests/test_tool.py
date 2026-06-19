import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.services.tool_service import get_weather, get_order_status, execute_tool_call


@pytest.mark.asyncio
async def test_get_weather():
    """测试原生本地工具：查天气（已改为异步，mock httpx 避免真实网络请求）"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "1",
        "lives": [{
            "weather": "晴",
            "temperature": "25",
            "winddirection": "东南风",
            "humidity": "60%"
        }]
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        result = await get_weather("北京")

    assert "晴" in result


@pytest.mark.asyncio
async def test_get_weather_invalid_city():
    """测试查天气——不存在的城市应返回友好提示"""
    result = await get_weather("火星")
    assert "无法" in result or "抱歉" in result


@pytest.mark.asyncio
async def test_get_order_status():
    """测试原生本地工具：查订单（已改为异步）"""
    result = await get_order_status("12345")
    assert "顺丰快递" in result

    result = await get_order_status("99999")
    assert "抱歉" in result


@pytest.mark.asyncio
async def test_execute_tool_call():
    """
    测试高级特工(Agent)逻辑：模拟大模型分析得出 Tool_Call，
    然后验证本地执行是否闭环，再传回给大模型说话。

    注意：AsyncOpenAI 的 create 是异步方法，mock 需要用 AsyncMock 的 side_effect。
    """
    with patch("app.services.tool_service.client.chat.completions.create", new_callable=AsyncMock) as mock_create:
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

        # 第二阶段：模拟大模型的第二回合润色回答
        mock_message_2 = MagicMock()
        mock_message_2.tool_calls = None
        mock_message_2.content = "您的订单12345已发货，由顺丰快递派送。"

        mock_response_2 = MagicMock()
        mock_response_2.choices = [MagicMock(message=mock_message_2)]

        # 用 AsyncMock 包装 side_effect，使其支持 await
        mock_create.side_effect = [mock_response_1, mock_response_2]

        # 执行业务逻辑（现在 execute_tool_call 是 async 的）
        result = await execute_tool_call("查一下订单12345")

        # 执行断言
        assert "顺丰快递" in result
        assert "[来源：外部微服务/API工具聚合]" in result
        assert mock_create.call_count == 2
