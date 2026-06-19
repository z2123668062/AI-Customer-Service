# app/api/v1/endpoints/sessions.py
from fastapi import APIRouter, HTTPException, Depends
from app.models.schemas import SessionCreateRequest, SessionUpdateRequest, SessionResponse
from app.services import session_service
from app.services.auth_service import decode_token
from app.services.ratelimit_service import check_session_limit
from fastapi import Header

router = APIRouter()


def _get_user_id(authorization: str = Header(...)) -> int:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供有效的认证Token")
    token = authorization[7:]
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token已过期或无效")
    return payload["user_id"]


@router.post("/", response_model=SessionResponse)
async def create_session(req: SessionCreateRequest, user_id: int = Depends(_get_user_id)):
    if not await check_session_limit(user_id):
        raise HTTPException(status_code=429, detail="创建会话太频繁了，请稍后再试。")
    session = await session_service.create_session(user_id, req.title or "新对话")
    return SessionResponse(
        session_id=session["session_id"],
        title=session["title"],
        created_at=""
    )


@router.get("/", response_model=list[SessionResponse])
async def list_sessions(user_id: int = Depends(_get_user_id)):
    sessions = await session_service.list_sessions(user_id)
    return [SessionResponse(
        session_id=s["session_id"],
        title=s.get("title", "新对话"),
        created_at=s.get("created_at", "")
    ) for s in sessions]


@router.patch("/{session_id}")
async def update_session(session_id: str, req: SessionUpdateRequest, user_id: int = Depends(_get_user_id)):
    ok = await session_service.update_session_title(user_id, session_id, req.title)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "更新成功"}


@router.delete("/{session_id}")
async def delete_session(session_id: str, user_id: int = Depends(_get_user_id)):
    await session_service.delete_session(user_id, session_id)
    return {"message": "删除成功"}