# app/api/v1/endpoints/auth.py
from fastapi import APIRouter, HTTPException
from app.models.schemas import (
    UserRegisterRequest, UserLoginRequest, UserResponse,
    TokenResponse, SendCodeRequest, PhoneLoginRequest
)
from app.services.auth_service import (
    register_user, login_user, send_sms_code,
    verify_sms_code, login_or_register_by_phone, create_token, decode_token
)

router = APIRouter()


@router.post("/register", response_model=TokenResponse)
async def register(req: UserRegisterRequest):
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
async def login(req: UserLoginRequest):
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
async def send_code(req: SendCodeRequest):
    send_sms_code(req.phone)
    return {"message": "验证码已发送（控制台查看）", "phone": req.phone}


@router.post("/phone-login", response_model=TokenResponse)
async def phone_login(req: PhoneLoginRequest):
    if not verify_sms_code(req.phone, req.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    user = await login_or_register_by_phone(req.phone)
    token = create_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user.id, username=user.username, phone=user.phone)
    )