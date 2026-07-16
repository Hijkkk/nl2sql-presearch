"""Async CRUD functions for conversation history."""
from typing import Optional
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.models.conversation import (
    Conversation,
    ConversationCreate,
    ConversationDetail,
    ConversationMessage,
    ConversationMessageOut,
    ConversationSummary,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def to_summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        data_source=conversation.data_source,
        model_id=conversation.model_id,
        message_count=conversation.message_count,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def to_message_out(message: ConversationMessage) -> ConversationMessageOut:
    return ConversationMessageOut(
        id=message.id,
        role=message.role,
        content=message.content,
        sql=message.sql_text,
        columns=message.columns_json,
        results=message.results_json,
        row_count=message.row_count,
        execution_time=message.execution_time,
        insight=message.insight,
        success=message.success,
        error=message.error_message,
        created_at=message.created_at,
    )


def to_detail(conversation: Conversation) -> ConversationDetail:
    messages = sorted(conversation.messages, key=lambda item: item.created_at)
    return ConversationDetail(
        **to_summary(conversation).model_dump(),
        model_conf=conversation.model_config,
        last_sql=conversation.last_sql,
        messages=[to_message_out(message) for message in messages],
    )


async def list_conversations(
    db: AsyncSession,
    user_id: int,
    page: int,
    page_size: int,
) -> tuple[list[ConversationSummary], int]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    offset = (page - 1) * page_size

    total_stmt = select(func.count()).select_from(Conversation).where(
        Conversation.user_id == user_id
    )
    total = await db.scalar(total_stmt) or 0

    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = (await db.scalars(stmt)).all()
    return [to_summary(item) for item in rows], total


async def create_conversation(
    db: AsyncSession,
    user_id: int,
    payload: ConversationCreate,
) -> ConversationSummary:
    conversation = Conversation(
        id=_new_id("conv"),
        user_id=user_id,
        title=payload.title.strip(),
        data_source=payload.data_source,
        model_id=payload.model_id,
        model_config=payload.model_conf,
        message_count=0,
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return to_summary(conversation)


async def get_conversation(
    db: AsyncSession,
    user_id: int,
    conversation_id: str,
) -> Optional[ConversationDetail]:
    stmt = (
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    conversation = await db.scalar(stmt)
    if not conversation:
        return None
    return to_detail(conversation)


async def delete_conversation(
    db: AsyncSession,
    user_id: int,
    conversation_id: str,
) -> bool:
    stmt = delete(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
    )
    result = await db.execute(stmt)
    await db.commit()
    return bool(result.rowcount)


async def save_chat_exchange(
    db: AsyncSession,
    user_id: int,
    conversation_id: Optional[str],
    question: str,
    data_source: str,
    model_id: Optional[str],
    model_config: Optional[dict],
    ai_content: str,
    sql: Optional[str],
    columns: Optional[list],
    results: Optional[list[dict]],
    row_count: Optional[int],
    execution_time: Optional[float],
    insight: Optional[str],
    success: bool,
    error: Optional[str],
) -> tuple[str, str]:
    """Persist one user question and one AI answer into conversation history."""
    conversation: Optional[Conversation] = None
    if conversation_id:
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        conversation = await db.scalar(stmt)

    if not conversation:
        conversation = Conversation(
            id=_new_id("conv"),
            user_id=user_id,
            title=question[:40],
            data_source=data_source,
            model_id=model_id or "qwen3-coder-next-fp8",
            model_config=model_config,
            message_count=0,
        )
        db.add(conversation)
        await db.flush()

    user_message = ConversationMessage(
        id=_new_id("msg"),
        conversation_id=conversation.id,
        user_id=user_id,
        role="user",
        content=question,
    )
    ai_message = ConversationMessage(
        id=_new_id("msg"),
        conversation_id=conversation.id,
        user_id=user_id,
        role="ai",
        content=ai_content,
        sql_text=sql,
        columns_json=columns,
        results_json=results,
        row_count=row_count,
        execution_time=execution_time,
        insight=insight,
        success=success,
        error_message=error,
    )
    db.add_all([user_message, ai_message])

    conversation.last_sql = sql
    conversation.message_count = conversation.message_count + 2
    conversation.data_source = data_source
    conversation.model_id = model_id or conversation.model_id
    conversation.model_config = model_config or conversation.model_config

    await db.commit()
    return conversation.id, ai_message.id
