import pytest
from app.services.safety_service import sensitive_trie, normalize_text, check_input_safety
import asyncio

def test_normalize_text():
    # 测试清洗功能
    assert normalize_text("作*  弊") == "作弊"
    assert normalize_text("违 禁!词 1") == "违禁词1"
    assert normalize_text("hello 123 !") == "hello123"

def test_check_input_safety():
    # 测试未包含敏感词
    assert asyncio.run(check_input_safety("这是一条正常的文本，今天天气不错")) == True

    # 测试包含敏感词
    assert asyncio.run(check_input_safety("你怎么在这里作弊啊")) == False

    # 测试绕过手段组合
    assert asyncio.run(check_input_safety("你这叫不 合...规*")) == False

def test_trie_search():
    # 测试字典树核心功能
    assert sensitive_trie.search_any("这是一条正常的文本") == False
    assert sensitive_trie.search_any("今天有人作弊") == True

