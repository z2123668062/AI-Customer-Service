import pytest
from unittest.mock import patch, MagicMock
from app.services.rag_service import query_knowledge

@patch("app.services.rag_service.build_or_load_index")
def test_query_knowledge(mock_build_index):
    """
    测试 RAG 服务：它是否能正确调起索引查询引擎，并正确返回带后缀格式的字符串，
    同时在测试期间不产生真实的各种大模型库读取。
    """
    # 1. 制作一个假的查询响应对象。
    # 由于真实业务代码中使用了 str(response)，我们需要模拟对象被转为字符串时的结果。
    mock_response = MagicMock()
    mock_response.__str__.return_value = "出差补贴每天200元。"
    
    # 2. 制作一个假的查询引擎
    mock_query_engine = MagicMock()
    mock_query_engine.query.return_value = mock_response
    
    # 3. 制作一个假的知识库（索引）
    mock_index = MagicMock()
    mock_index.as_query_engine.return_value = mock_query_engine
    
    # 4. 把被 patch（拦截）的方法返回值替换成我们捏好的假知识库
    mock_build_index.return_value = mock_index

    # 5. 执行我们要测试的真实业务代码
    result = query_knowledge("二线城市补贴多少钱？")
    
    # 6. 断言判断
    assert "200元" in result
    assert "[来源：系统私有知识库]" in result
    # 验证是否成功带着用户问题进入了查询引擎
    mock_query_engine.query.assert_called_once_with("二线城市补贴多少钱？")
