"""
User authentication and user preference APIs.
身份验证和用户偏好设置 API。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
# 保留当前用户的默认模型配置。
v1_router = APIRouter(prefix="/api/v1/user", tags=["user"])
bearer_scheme = HTTPBearer(auto_error=False)


# 规范化授权令牌。
# 统一提取纯 token
#   客户端发送格式不统一
# "  Bearer abc123  "  →  strip()  →  "Bearer abc123"  →  去掉前缀  →  "abc123"
# "abc123"            →  strip()  →  "abc123"
# 去除首尾空格：防止因空格导致 token 匹配失败
# 大小写不敏感：Bearer 和 bearer 都能正确处理（token.lower().startswith("bearer ")）
# 空值处理：没有 Authorization 头时返回空字符串，后续统一判断并返回 401→  无前缀，直接返回  →  "abc123"

# HTTP Authorization 通用标准格式
# Authorization: <认证方案Scheme> <凭证Credentials>
# Bearer 只是其中一种 Scheme（认证类型标记），作用是告诉后端：后面这串字符串该用哪种逻辑解析。
def _normalize_token(authorization: Optional[str]) -> str:
    # 如果无授权头，则返回空字符串。
    if not authorization:
        return ""

    # 去除授权头前后的空格。
    token = authorization.strip()
    # 如果授权头以 "Bearer " 开头，则去除 "Bearer " 前缀。
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


async def get_current_user(
    # 来自 FastAPI 的 from fastapi import Header，用于获取请求头参数
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    # 依赖项，用于获取数据库会话
    db: AsyncSession = Depends(get_db),
) -> User:
    # 获取并规范化授权令牌。
    # Keep compatibility with the existing frontend's raw Authorization token,
    # while exposing a standard Bearer security scheme in Swagger UI.
    bearer_value = f"Bearer {credentials.credentials}" if credentials else None
    token = _normalize_token(authorization or bearer_value)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或缺少 Authorization",
        )

    # 根据令牌获取用户。
    user = await user_crud.get_user_by_token(db, token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 无效或已过期",
        )
    return user


# 创建登录响应。
def _auth_response(token: str, user: User, message: str) -> ApiResponse:
    data = AuthData(
        token=token,
        userInfo=user_crud.to_user_info(user),
    )
    # model_dump() 是 Pydantic 模型的一个方法，作用是把模型对象转换成 Python 字典（dict）。
    return ApiResponse(code=200, message=message, data=data.model_dump())


# 注册新用户并返回登录令牌。
@router.post("/register", response_model=ApiResponse)
async def register(payload: UserAuthRequest, db: AsyncSession = Depends(get_db)) -> ApiResponse:
    """
    Register a new user and return a login token.
    注册新用户并返回登录令牌
    """
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


# 登录并返回登录令牌。
# 生成全新的 token（旧的立即失效）
# 过期时间从登录时刻重新算 7 天
# 所以只要用户持续登录，token 就不会过期。只有连续 7 天不登录，token 才会真正失效。
@router.post("/login", response_model=ApiResponse)
async def login(payload: UserAuthRequest, db: AsyncSession = Depends(get_db)) -> ApiResponse:
    """
    Login with username and password.
    登录并返回登录令牌
    """
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


# 获取当前登录用户的信息。
@router.get("/info", response_model=ApiResponse)
async def get_user_info(current_user: User = Depends(get_current_user)) -> ApiResponse:
    """
    Return the current logged-in user.
    获取当前登录用户的信息 返回用户信息
    """
    return ApiResponse(
        code=200,
        message="success",
        data=user_crud.to_user_info(current_user).model_dump(),
    )


# 保存当前用户的默认模型配置。
@v1_router.put("/model-config", response_model=ApiResponse)
async def save_model_config(
    payload: UserModelConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    Persist the current user's default model configuration.
    保存当前用户的默认模型配置 返回保存结果
    """
    config = await user_crud.update_user_model_config(db, current_user, payload)
    return ApiResponse(
        code=200,
        message="保存成功",
        data=config.model_dump(),
    )
