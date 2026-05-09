import logging
import re

logger = logging.getLogger(__name__)


class TrieNode:
    def __init__(self):
        self.children = {}
        self.is_end_of_word = False


class SensitiveWordTrie:
    def __init__(self):
        self.root = TrieNode()

    def add_word(self, word: str):
        node = self.root
        for char in word:
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        node.is_end_of_word = True

    def search_any(self, text: str) -> bool:
        """
        在文本中查找是否包含任意敏感词
        时间复杂度与词库大小无关，只与文本长度有关！
        """
        length = len(text)
        for i in range(length):
            node = self.root
            for j in range(i, length):
                char = text[j]
                if char not in node.children:
                    break
                node = node.children[char]
                if node.is_end_of_word:
                    return True
        return False


# 初始化全局的字典树供服务使用
sensitive_trie = SensitiveWordTrie()
SENSITIVE_WORDS = ["作弊", "不合规", "违禁词1", "违规操作"]

# 服务启动时构建树（预编译）
for w in SENSITIVE_WORDS:
    sensitive_trie.add_word(w)


def normalize_text(text: str) -> str:
    """
    文本清洗：去除空格、标点符号、特殊字符
    防止用户通过 "作  弊" 或 "作*弊" 来绕过检测
    """
    # 仅保留中文字符、英文字母和数字
    return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', text)


async def check_input_safety(text: str) -> bool:
    """
    检查用户的输入是否安全合规
    """
    if not text:
        return True

    # 1. 文本预处理（降维打击用户的绕过手段）
    clean_text = normalize_text(text)

    # 2. 字典树 $O(N)$ 极速匹配
    if sensitive_trie.search_any(clean_text):
        logger.warning(f"触发安全审校拦截。原文: {text}")
        return False

    return True


async def get_safety_rejection_message() -> str:
    return "抱歉，您讨论的内容可能涉及敏感或违规信息，我无法为您解答该问题。"