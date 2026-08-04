"""Conversation history APIs."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.crud import conversation as conversation_crud
from backend.config.database import get_db
from backend.models.conversation import ConversationCreate, ConversationRename
from backend.models.user import ApiResponse, User
from backend.routers.user import get_current_user

router = APIRouter(prefix="/api/v1/conversations", tags=["conversation"])


@router.get("", response_model=ApiResponse)
async def list_conversations(
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    search: str = Query(default="", max_length=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    Return current user's conversation list.
    返回当前用户的对话列表。
    返回对话列表。
    """
    # 接受分页参数 每行信息 以及总条数
    rows, total = await conversation_crud.list_conversations(
        db=db,
        user_id=current_user.id,
        page=page,
        page_size=pageSize,
        search=search,
    )
    return ApiResponse(
        code=200,
        message="success",
        data={
            "list": [item.model_dump() for item in rows],
            "total": total,
            "hasMore": page * pageSize < total,
        },
    )


@router.post("", response_model=ApiResponse)
async def create_conversation(
    payload: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    Create a conversation before or during a chat.
    在聊天之前或期间创建一个对话。
    创建对话。
    """
    conversation = await conversation_crud.create_conversation(
        db=db,
        user_id=current_user.id,
        payload=payload,
    )
    return ApiResponse(
        code=200,
        message="创建成功",
        data=conversation.model_dump(),
    )


@router.get("/{conversation_id}", response_model=ApiResponse)
async def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    Return one conversation with messages.
    返回一个对话及其消息。
    获取对话。
    """
    conversation = await conversation_crud.get_conversation(
        db=db,
        user_id=current_user.id,
        conversation_id=conversation_id,
    )
    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )
    return ApiResponse(
        code=200,
        message="success",
        data=conversation.model_dump(),
    )


@router.put("/{conversation_id}", response_model=ApiResponse)
async def rename_conversation(
    conversation_id: str,
    payload: ConversationRename,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    conversation = await conversation_crud.rename_conversation(
        db=db,
        user_id=current_user.id,
        conversation_id=conversation_id,
        title=payload.title,
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return ApiResponse(code=200, message="重命名成功", data=conversation.model_dump())


@router.delete("/{conversation_id}", response_model=ApiResponse)
async def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    Delete one conversation owned by current user.
    删除当前用户拥有的一个对话。
    删除对话。
    """
    deleted = await conversation_crud.delete_conversation(
        db=db,
        user_id=current_user.id,
        conversation_id=conversation_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在",
        )
    return ApiResponse(code=200, message="删除成功", data=None)
