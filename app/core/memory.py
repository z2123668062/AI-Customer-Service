import json
from typing import List, Literal, Optional

from app.core import logging
from app.models.schemas import ChatMessage

from redis.asyncio import Redis
from app.core.config import settings
redis_client = Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB, decode_responses=True)
SLIDING_WINDOW_SIZE = 10


def _key(session_id: str, user_id: Optional[int] = None) -> str:
    if user_id:
        return f"chat:user:{user_id}:session:{session_id}"
    return f"chat:session:{session_id}"


async def add_message(session_id: str, role: Literal["user", "assistant"], content: str, user_id: Optional[int] = None) -> ChatMessage:
    message = ChatMessage(role=role, content=content)
    redis_key = _key(session_id, user_id)
    msg_json = message.model_dump_json()
    await redis_client.rpush(redis_key, msg_json)
    await redis_client.ltrim(redis_key, -SLIDING_WINDOW_SIZE, -1)
    return message


async def get_history(session_id: str, user_id: Optional[int] = None) -> List[ChatMessage]:
    redis_key = _key(session_id, user_id)
    raw_messages = await redis_client.lrange(redis_key, 0, -1)
    history = []
    for raw in raw_messages:
        data = json.loads(raw)
        history.append(ChatMessage(**data))
    return history


async def get_history_count(session_id: str, user_id: Optional[int] = None) -> int:
    redis_key = _key(session_id, user_id)
    return await redis_client.llen(redis_key)


async def undo_last_turn(session_id: str, user_id: Optional[int] = None) -> None:
    redis_key = _key(session_id, user_id)
    count = await redis_client.llen(redis_key)
    if count < 2:
        logging.logger.warning(f"会话 {session_id} 中没有足够的消息来撤销。")
        return
    await redis_client.rpop(redis_key)
    await redis_client.rpop(redis_key)