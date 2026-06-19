# app/api/v1/endpoints/auth.py
from fastapi import APIRouter, HTTPException, Request
from app.models.schemas import (
    UserRegisterRequest, UserLoginRequest, UserResponse,
    TokenResponse, SendCodeRequest, PhoneLoginRequest
)
from app.services.auth_service import (
    register_user, login_user, send_sms_code,
    verify_sms_code, login_or_register_by_phone, create_token, decode_token
)
from app.core.memory import redis_client

router = APIRouter()

AUTH_RATE_LIMIT = 5
AUTH_WINDOW_SECONDS = 60
SEND_CODE_RATE_LIMIT = 3

def _ip_key(ip: str, endpoint: str) -> str:
    return f"ratelimit:auth:{endpoint}:{ip}"


async def _check_ip_limit(ip: str, endpoint: str,max_request:int =5) -> bool:
    key = _ip_key(ip, endpoint)
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, AUTH_WINDOW_SECONDS)
    return count <= max_request


@router.post("/register", response_model=TokenResponse)
async def register(req: UserRegisterRequest, request: Request):
    ip = request.client.host
    if not await _check_ip_limit(ip, "register"):
        raise HTTPException(status_code=429, detail="注册请求太频繁，请稍后再试。")
    try:
        user = await register_user(req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = create_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user.id, username=user.username, phone=user.phone)
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: UserLoginRequest, request: Request):
    ip = request.client.host
    if not await _check_ip_limit(ip, "login"):
        raise HTTPException(status_code=429, detail="登录请求太频繁，请稍后再试。")
    try:
        user = await login_user(req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    token = create_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user.id, username=user.username, phone=user.phone)
    )


@router.post("/send-code")
async def send_code(req: SendCodeRequest, request: Request):
    ip=request.client.host
    if not await _check_ip_limit(ip, "send-code",SEND_CODE_RATE_LIMIT):
        raise HTTPException(status_code=429, detail="发送验证码请求太频繁，请稍后再试。")
    send_sms_code(req.phone)
    return {"message": "验证码已发送（控制台查看）", "phone": req.phone}

@router.post("/phone-login", response_model=TokenResponse)
async def phone_login(req: PhoneLoginRequest, request: Request):
    ip=request.client.host
    if not await _check_ip_limit(ip, "phone-login"):
        raise HTTPException(status_code=429, detail="手机号登录请求太频繁，请稍后再试。")
    if not verify_sms_code(req.phone, req.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    user = await login_or_register_by_phone(req.phone)
    token = create_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user.id, username=user.username, phone=user.phone)
    )