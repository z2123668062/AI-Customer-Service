# app/services/session_service.py
import uuid
import json
from datetime import datetime, timezone
from app.core.memory import redis_client


def generate_session_id() -> str:
    return uuid.uuid4().hex[:12]


def _session_key(user_id: int, session_id: str) -> str:
    return f"chat:user:{user_id}:session:{session_id}"


def _session_meta_key(user_id: int) -> str:
    return f"chat:user:{user_id}:sessions"


def create_session(user_id: int, title: str = "新对话") -> dict:
    session_id = generate_session_id()
    redis_client.hset(
        _session_meta_key(user_id),
        session_id,
        json.dumps({"title": title, "created_at": datetime.now(timezone.utc).isoformat()})
    )
    return {"session_id": session_id, "title": title}


def list_sessions(user_id: int) -> list[dict]:
    raw = redis_client.hgetall(_session_meta_key(user_id))
    sessions = []
    for session_id, data_json in raw.items():
        data = json.loads(data_json)
        sessions.append({"session_id": session_id, **data})
    sessions.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return sessions


def update_session_title(user_id: int, session_id: str, title: str) -> bool:
    raw = redis_client.hget(_session_meta_key(user_id), session_id)
    if not raw:
        return False
    data = json.loads(raw)
    data["title"] = title
    redis_client.hset(_session_meta_key(user_id), session_id, json.dumps(data))
    return True


def delete_session(user_id: int, session_id: str) -> bool:
    redis_client.delete(_session_key(user_id, session_id))
    redis_client.hdel(_session_meta_key(user_id), session_id)
    return True