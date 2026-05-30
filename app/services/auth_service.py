# app/services/auth_service.py
import random
import json
import jwt
from datetime import datetime, timedelta, timezone
from bcrypt import hashpw, gensalt, checkpw
from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.core.config import settings
from app.models.domain import User
from app.core.memory import redis_client

JWT_SECRET_KEY = settings.ZHIPU_API_KEY[::-1]
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24
SMS_CODE_EXPIRE_SECONDS = 300


def hash_password(password: str) -> str:
    return hashpw(password.encode("utf-8"), gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_token(user_id: int, username: str) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


async def register_user(username: str, password: str) -> User:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none():
            raise ValueError("用户名已存在")
        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def login_user(username: str, password: str) -> User:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if not user or not verify_password(password, user.password_hash):
            raise ValueError("用户名或密码错误")
        return user


def send_sms_code(phone: str) -> str:
    code = f"{random.randint(100000, 999999)}"
    redis_client.setex(f"sms:code:{phone}", SMS_CODE_EXPIRE_SECONDS, code)
    print(f"\n[验证码 Mock] 手机号 {phone} 的验证码是: {code}\n")
    return code


def verify_sms_code(phone: str, code: str) -> bool:
    stored = redis_client.get(f"sms:code:{phone}")
    if not stored:
        return False
    if stored != code:
        return False
    redis_client.delete(f"sms:code:{phone}")
    return True


async def login_or_register_by_phone(phone: str) -> User:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.phone == phone))
        user = result.scalar_one_or_none()
        if user:
            return user
        username = f"user_{phone[-4:]}"
        user = User(username=username, password_hash=hash_password(phone[::-1]), phone=phone)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user