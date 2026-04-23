from typing import Dict,List,Literal

from app.models.schemas import ChatMessage

#定义一个空字典来存聊天记录，键是session_id，值是ChatMessage列表
_sessions:Dict[str,List[ChatMessage]] = {}

def add_message(session_id:str,role:Literal["user","assistant"],content:str)->ChatMessage:
    """向指定的会话里面追加存入一条新消息"""

    # 1. 组装成标准格式：用我们之前在 schemas 里定义的 ChatMessage 模型
    message = ChatMessage(role=role, content=content)

    # 2. 如果这是这个用户的第一次聊天，字典里还没有他的记录
    # 我们就先给他创建一个“空列表”来装消息
    if session_id not in _sessions:
        _sessions[session_id] = []

    # 3. 把这条新消息追加到他的专属列表里
    _sessions[session_id].append(message)

    # 4. 把组装好的消息返回出去，方便后面别的代码直接用
    return message

def get_history(session_id: str) -> List[ChatMessage]:
    """获取指定会话的所有历史消息"""
    # 字典的 .get() 方法很安全：如果找不到这个 session_id，就返回默认值（这里是一个空列表 []）
    return _sessions.get(session_id, [])


def get_history_count(session_id: str) -> int:
    """获取指定会话中总共有几条消息"""
    # 先利用上面写的函数把历史拿出来
    history = get_history(session_id)
    # len() 是 Python 内置的数个数的方法
    return len(history)