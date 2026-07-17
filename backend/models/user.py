"""
User ORM entity and API schemas.
"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.config.database import Base


DEFAULT_AVATAR = "https://fastly.jsdelivr.net/npm/@vant/assets/cat.jpeg"
DEFAULT_BIO = "这个人很懒，什么都没留下"


class User(Base):
    """Application user stored in MySQL."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    nickname: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar: Mapped[str] = mapped_column(String(512), default=DEFAULT_AVATAR, nullable=False)
    gender: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    bio: Mapped[str] = mapped_column(String(255), default=DEFAULT_BIO, nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    token_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    model_id: Mapped[str] = mapped_column(String(128), default="qwen3-coder-next-fp8", nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    top_p: Mapped[float] = mapped_column(Float, default=0.8, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048, nullable=False)
    enable_sql_safety: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    return_insight: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ApiResponse(BaseModel):
    code: int = 200
    message: str = "success"
    data: Any = None


class UserAuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, description="用户名")
    password: str = Field(..., min_length=6, max_length=128, description="密码")


class UserInfo(BaseModel):
    id: int
    username: str
    nickname: str
    avatar: str
    gender: str
    bio: str
    phone: Optional[str] = None


class AuthData(BaseModel):
    token: str
    userInfo: UserInfo


class UserModelConfigUpdate(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    temperature: float = Field(0.2, ge=0, le=1)
    top_p: float = Field(0.8, ge=0.1, le=1)
    max_tokens: int = Field(2048, ge=128, le=32768)
    enable_sql_safety: bool = True
    return_insight: bool = True


class UserModelConfig(BaseModel):
    model_id: str
    temperature: float
    top_p: float
    max_tokens: int
    enable_sql_safety: bool
    return_insight: bool
    updated_at: datetime
