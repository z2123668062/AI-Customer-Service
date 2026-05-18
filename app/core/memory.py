import json
import redis
from typing import List, Literal

from app.core import logging
from app.models.schemas import ChatMessage

# 【配置连接】连通我们刚刚启动的本地 Redis
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

# 【滑动窗口】限制只给大模型传最近 10 条（5轮）对话，保护 Token
SLIDING_WINDOW_SIZE = 10


def add_message(session_id: str, role: Literal["user", "assistant"], content: str) -> ChatMessage:
    """向指定的会话里面追加存入一条新消息（Redis 持久化 & 滑动窗口）"""
    message = ChatMessage(role=role, content=content)

    # 构造格式化的 Redis 键名
    redis_key = f"chat:session:{session_id}"

    # 1. 将 Pydantic 对象序列化为 JSON 字符串
    msg_json = message.model_dump_json()

    # 2. 追加到 Redis 列表最右侧
    redis_client.rpush(redis_key, msg_json)

    # 3. 执行滑动窗口裁切，只要倒数第 SLIDING_WINDOW_SIZE 个到最后一个
    redis_client.ltrim(redis_key, -SLIDING_WINDOW_SIZE, -1)

    return message


def get_history(session_id: str) -> List[ChatMessage]:
    """获取指定会话的所有历史消息（从 Redis 中反序列化读取）"""
    redis_key = f"chat:session:{session_id}"

    # 获取这个键范围内的所有元素
    raw_messages = redis_client.lrange(redis_key, 0, -1)

    # 把 JSON 重新变回 ChatMessage 对象
    history = []
    for raw in raw_messages:
        data = json.loads(raw)
        history.append(ChatMessage(**data))

    return history


def get_history_count(session_id: str) -> int:
    """获取指定会话中总共有几条消息"""
    redis_key = f"chat:session:{session_id}"
    # 【性能优化】不需要像以前那样把真实数据全拉出来再查长度
    # Redis 提供了快速查列表长度的指令：llen
    return redis_client.llen(redis_key)

def undo_last_turn(session_id: str) -> None:
    """撤销指定会话的最后一轮对话（用户+助手各一条）"""
    redis_key = f"chat:session:{session_id}"
    #1.判断是否有两条消息：
    if redis_client.llen(redis_key) < 2:
        logging.logger.warning(f"会话 {session_id} 中没有足够的消息来撤销。")
        return
    redis_client.rpop(redis_key)  # 删除助手的最后一条消息
    redis_client.rpop(redis_key)  # 删除用户的最后一条消息
    return