import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.services.rag_service import query_knowledge


@pytest.mark.asyncio
async def test_query_knowledge():
    """
    测试 RAG 服务：它是否能正确调起索引查询引擎，并正确返回带后缀格式的字符串，
    同时在测试期间不产生真实的各种大模型库读取。

    注意：query_knowledge 现在是 async def，内部用 asyncio.to_thread 包裹了同步的 query_engine.query。
    测试中我们需要 mock get_readonly_index 和 asyncio.to_thread。
    """
    # 1. 制作一个假的查询响应对象。
    mock_response = MagicMock()
    mock_response.__str__.return_value = "出差补贴每天200元。"

    # 2. 制作一个假的查询引擎
    mock_query_engine = MagicMock()
    mock_query_engine.query.return_value = mock_response

    # 3. 制作一个假的知识库（索引）
    mock_index = MagicMock()
    mock_index.as_query_engine.return_value = mock_query_engine

    # 4. 同时 mock get_readonly_index 和 asyncio.to_thread
    with patch("app.services.rag_service.get_readonly_index", return_value=mock_index), \
         patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:

        # to_thread 应该返回 mock_response，因为 query_engine.query 被包裹在 to_thread 里
        mock_to_thread.return_value = mock_response

        # 5. 执行我们要测试的真实业务代码（现在是 async）
        result = await query_knowledge("二线城市补贴多少钱？")

        # 6. 断言判断
        assert "200元" in result
        assert "[来源：系统经过精准重排提取]" in result

        # 验证 to_thread 被正确调用——query_engine.query 被包裹了进去
        mock_to_thread.assert_awaited_once()
