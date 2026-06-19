import pytest
from app.services.safety_service import sensitive_trie, normalize_text, check_input_safety


def test_normalize_text():
    """测试清洗功能（同步函数，不需要 asyncio）"""
    assert normalize_text("作*  弊") == "作弊"
    assert normalize_text("违 禁!词 1") == "违禁词1"
    assert normalize_text("hello 123 !") == "hello123"


@pytest.mark.asyncio
async def test_check_input_safety():
    """测试输入安全检测（async 函数，用 await 调用）"""
    # 测试未包含敏感词
    assert await check_input_safety("这是一条正常的文本，今天天气不错") is True

    # 测试包含敏感词
    assert await check_input_safety("你怎么在这里作弊啊") is False

    # 测试绕过手段组合
    assert await check_input_safety("你这叫不 合...规*") is False


def test_trie_search():
    """测试字典树核心功能（同步函数，不需要 asyncio）"""
    assert sensitive_trie.search_any("这是一条正常的文本") is False
    assert sensitive_trie.search_any("今天有人作弊") is True
