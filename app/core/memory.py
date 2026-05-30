import json
import redis
from typing import List, Literal, Optional

from app.core import logging
from app.models.schemas import ChatMessage

redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
SLIDING_WINDOW_SIZE = 10


def _key(session_id: str, user_id: Optional[int] = None) -> str:
    if user_id:
        return f"chat:user:{user_id}:session:{session_id}"
    return f"chat:session:{session_id}"


def add_message(session_id: str, role: Literal["user", "assistant"], content: str, user_id: Optional[int] = None) -> ChatMessage:
    message = ChatMessage(role=role, content=content)
    redis_key = _key(session_id, user_id)
    msg_json = message.model_dump_json()
    redis_client.rpush(redis_key, msg_json)
    redis_client.ltrim(redis_key, -SLIDING_WINDOW_SIZE, -1)
    return message


def get_history(session_id: str, user_id: Optional[int] = None) -> List[ChatMessage]:
    redis_key = _key(session_id, user_id)
    raw_messages = redis_client.lrange(redis_key, 0, -1)
    history = []
    for raw in raw_messages:
        data = json.loads(raw)
        history.append(ChatMessage(**data))
    return history


def get_history_count(session_id: str, user_id: Optional[int] = None) -> int:
    redis_key = _key(session_id, user_id)
    return redis_client.llen(redis_key)


def undo_last_turn(session_id: str, user_id: Optional[int] = None) -> None:
    redis_key = _key(session_id, user_id)
    if redis_client.llen(redis_key) < 2:
        logging.logger.warning(f"会话 {session_id} 中没有足够的消息来撤销。")
        return
    redis_client.rpop(redis_key)
    redis_client.rpop(redis_key)