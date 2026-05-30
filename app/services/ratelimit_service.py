# app/services/ratelimit_service.py
from app.core.memory import redis_client


RATE_LIMIT_CHAT = 20
RATE_LIMIT_SESSION = 5
WINDOW_SECONDS = 60


def _minute_key(prefix: str, identifier: str) -> str:
    import datetime
    minute = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M")
    return f"ratelimit:{prefix}:{identifier}:{minute}"


def check_chat_limit(user_id: int) -> bool:
    key = _minute_key("chat", str(user_id))
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, WINDOW_SECONDS)
    return count <= RATE_LIMIT_CHAT


def check_session_limit(user_id: int) -> bool:
    key = _minute_key("session", str(user_id))
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, WINDOW_SECONDS)
    return count <= RATE_LIMIT_SESSION