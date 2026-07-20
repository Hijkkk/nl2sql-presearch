"""
CRUD functions for users.
"""
from datetime import datetime, timedelta
import hashlib
import hmac
import secrets
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.user import (
    DEFAULT_AVATAR,
    DEFAULT_BIO,
    User,
    UserInfo,
    UserModelConfig,
    UserModelConfigUpdate,
)

PASSWORD_ITERATIONS = 260_000
TOKEN_EXPIRE_DAYS = 7


def _hash_token(token: str) -> str:
    # _hash_token 的作用
    # 这个函数用 SHA-256 算法对 token 字符串进行哈希（摘要）计算，
    # 把任意长度的 token 转换成一个固定长度的 64 位十六进制字符串。
    # 数据库被泄露
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Hash a plaintext password with PBKDF2-HMAC-SHA256."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against the stored password hash."""
    try:
        algorithm, iterations_text, salt, expected_digest = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False

        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations_text),
        ).hex()
        return hmac.compare_digest(digest, expected_digest)
    except Exception:
        return False


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[User]:
    stmt = select(User).where(User.username == username)
    return await db.scalar(stmt)


async def create_user(db: AsyncSession, username: str, password: str) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        nickname=username,
        avatar=DEFAULT_AVATAR,
        gender="unknown",
        bio=DEFAULT_BIO,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, username: str, password: str) -> Optional[User]:
    user = await get_user_by_username(db, username)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def issue_access_token(db: AsyncSession, user: User) -> str:
    """Create a frontend-compatible token and store only its hash."""
    token = secrets.token_urlsafe(32)
    user.token_hash = _hash_token(token)
    user.token_expires_at = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return token


async def get_user_by_token(db: AsyncSession, token: str) -> Optional[User]:
    # is_active 是用户表中的一个布尔字段，表示该用户账号是否处于激活/启用状态
    token_hash = _hash_token(token)
    stmt = select(User).where(
        User.token_hash == token_hash,
        # is_active 字段用于判断用户账号是否处于激活/启用状态
        User.is_active.is_(True),
    )
    user = await db.scalar(stmt)
    # 如果用户不存在或令牌已过期，则返回 None
    # 防御性编程 防止用户不存在或令牌已过期
    # 数据库是可以被外部直接修改的
    if not user or not user.token_expires_at:
        return None
    # 如果令牌已过期，则返回 None
    if user.token_expires_at < datetime.utcnow():
        return None
    return user


def to_user_info(user: User) -> UserInfo:
    return UserInfo(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        avatar=user.avatar,
        gender=user.gender,
        bio=user.bio,
        phone=user.phone,
    )


async def update_user_model_config(
    db: AsyncSession,
    user: User,
    config: UserModelConfigUpdate,
) -> UserModelConfig:
    user.model_id = config.model_id
    user.temperature = config.temperature
    user.top_p = config.top_p
    user.max_tokens = config.max_tokens
    user.enable_sql_safety = config.enable_sql_safety
    user.return_insight = config.return_insight
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return to_model_config(user)


def to_model_config(user: User) -> UserModelConfig:
    return UserModelConfig(
        model_id=user.model_id,
        temperature=user.temperature,
        top_p=user.top_p,
        max_tokens=user.max_tokens,
        enable_sql_safety=user.enable_sql_safety,
        return_insight=user.return_insight,
        updated_at=user.updated_at,
    )
