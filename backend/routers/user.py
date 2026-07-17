"""
User authentication and user preference APIs.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.crud import user as user_crud
from backend.config.database import get_db
from backend.models.user import (
    ApiResponse,
    AuthData,
    User,
    UserAuthRequest,
    UserModelConfigUpdate,
)

router = APIRouter(prefix="/api/user", tags=["user"])
v1_router = APIRouter(prefix="/api/v1/user", tags=["user"])


def _normalize_token(authorization: Optional[str]) -> str:
    if not authorization:
        return ""

    token = authorization.strip()
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


async def get_current_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = _normalize_token(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或缺少 Authorization",
        )

    user = await user_crud.get_user_by_token(db, token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 无效或已过期",
        )
    return user


def _auth_response(token: str, user: User, message: str) -> ApiResponse:
    data = AuthData(
        token=token,
        userInfo=user_crud.to_user_info(user),
    )
    return ApiResponse(code=200, message=message, data=data.model_dump())


@router.post("/register", response_model=ApiResponse)
async def register(payload: UserAuthRequest, db: AsyncSession = Depends(get_db)) -> ApiResponse:
    """Register a new user and return a login token."""
    username = payload.username.strip()
    if await user_crud.get_user_by_username(db, username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

    try:
        user = await user_crud.create_user(db, username=username, password=payload.password)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在") from exc

    token = await user_crud.issue_access_token(db, user)
    return _auth_response(token, user, "注册成功")


@router.post("/login", response_model=ApiResponse)
async def login(payload: UserAuthRequest, db: AsyncSession = Depends(get_db)) -> ApiResponse:
    """Login with username and password."""
    user = await user_crud.authenticate_user(
        db,
        username=payload.username.strip(),
        password=payload.password,
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = await user_crud.issue_access_token(db, user)
    return _auth_response(token, user, "登录成功")


@router.get("/info", response_model=ApiResponse)
async def get_user_info(current_user: User = Depends(get_current_user)) -> ApiResponse:
    """Return the current logged-in user."""
    return ApiResponse(
        code=200,
        message="success",
        data=user_crud.to_user_info(current_user).model_dump(),
    )


@v1_router.put("/model-config", response_model=ApiResponse)
async def save_model_config(
    payload: UserModelConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """Persist the current user's default model configuration."""
    config = await user_crud.update_user_model_config(db, current_user, payload)
    return ApiResponse(
        code=200,
        message="保存成功",
        data=config.model_dump(),
    )
