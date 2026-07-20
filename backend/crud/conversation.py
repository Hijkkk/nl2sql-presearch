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
    # 排序依据：每条消息的 created_at 时间
    messages = sorted(conversation.messages, key=lambda item: item.created_at)
    return ConversationDetail(
        **to_summary(conversation).model_dump(),
        model_conf=conversation.model_config,
        last_sql=conversation.last_sql,
        messages=[to_message_out(message) for message in messages],
    )
# {
#   "id": "conv_abc123",
#   "title": "查询IT部门员工",
#   "data_source": "sqlite_demo",
#   "model_id": "qwen3-coder-next-fp8",
#   "message_count": 4,
#   "created_at": "2026-07-20T10:00:00",
#   "updated_at": "2026-07-20T10:35:00",
#
#   "model_conf": {
#     "temperature": 0.2,
#     "top_p": 0.8,
#     "max_tokens": 2048,
#     "enable_sql_safety": true
#   },
#
#   "last_sql": "SELECT * FROM sales WHERE dept='IT'",
#
#   "messages": [
#     {
#       "id": "msg_001",
#       "role": "user",
#       "content": "查询IT部门有多少员工",
#       "sql": null,
#       "columns": null,
#       "results": null,
#       "row_count": null,
#       "execution_time": null,
#       "insight": null,
#       "success": null,
#       "error": null,
#       "created_at": "2026-07-20T10:00:00"
#     },
#     {
#       "id": "msg_002",
#       "role": "assistant",
#       "content": "IT部门共有 15 名员工。",
#       "sql": "SELECT COUNT(*) FROM employees WHERE dept='IT'",
#       "columns": ["COUNT(*)"],
#       "results": [{"COUNT(*)": 15}],
#       "row_count": 1,
#       "execution_time": 0.023,
#       "insight": "IT部门人数适中",
#       "success": true,
#       "error": null,
#       "created_at": "2026-07-20T10:00:05"
#     },
#     {
#       "id": "msg_003",
#       "role": "user",
#       "content": "再查一下销售部的",
#       "sql": null,
#       "columns": null,
#       "results": null,
#       "row_count": null,
#       "execution_time": null,
#       "insight": null,
#       "success": null,
#       "error": null,
#       "created_at": "2026-07-20T10:30:00"
#     },
#     {
#       "id": "msg_004",
#       "role": "assistant",
#       "content": "销售部共有 28 名员工。",
#       "sql": "SELECT COUNT(*) FROM employees WHERE dept='销售'",
#       "columns": ["COUNT(*)"],
#       "results": [{"COUNT(*)": 28}],
#       "row_count": 1,
#       "execution_time": 0.018,
#       "insight": "销售部人数多于IT部门",
#       "success": true,
#       "error": null,
#       "created_at": "2026-07-20T10:30:05"
#     }
#   ]
# }


async def list_conversations(
    db: AsyncSession,
    user_id: int,
    page: int,
    page_size: int,
) -> tuple[list[ConversationSummary], int]:
    # 防止前端传恶意参数（如 page=-999、page_size=100000）导致数据库压力过大或返回异常数据。
    # 防止页码 ≤ 0，最小取 1
    page = max(page, 1)
    # 防止每页数量 ≤ 0，最小取 1，最大取 100
    page_size = min(max(page_size, 1), 100)
    # 计算分页偏移量
    offset = (page - 1) * page_size
    # 查询总数
    total_stmt = select(func.count()).select_from(Conversation).where(
        Conversation.user_id == user_id
    )
    # 执行查询
    total = await db.scalar(total_stmt) or 0

    # 查询分页数据
    # 优先按 updated_at 降序：最近有更新的对话排在最前面（比如刚发了新消息的会话）
    # 相同则按 created_at 降序：如果更新时间一样，新建的排前面
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = (await db.scalars(stmt)).all()
    # 对每条数据使用 to_summary 函数进行转换封装
    # total 是总数
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
    # @todo
    stmt = (
        select(Conversation)
        # 表示 Conversation（会话）和 ConversationMessage（消息）之间的一对多关系。
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    # 实际执行的 SQL（两条）
    # -- 第1条：查会话
    # SELECT * FROM conversation WHERE id = 1 AND user_id = 5;
    #
    # -- 第2条：查该会话的所有消息（selectinload 自动发起）
    # SELECT * FROM conversation_message WHERE conversation_id = 1;

    # Conversation
    # ├── id: 1
    # ├── user_id: 5
    # ├── title: "查询IT部门员工"
    # ├── data_source: "sqlite_demo"
    # ├── created_at: 2026-07-20 10:00:00
    # ├── updated_at: 2026-07-20 10:30:00
    # │
    # └── messages: [                          ← 自动加载的消息列表
    #         ConversationMessage(id=1, role="user",      content="查询IT部门有多少员工"),
    #         ConversationMessage(id=2, role="assistant",  content="SELECT COUNT(*)..."),
    #         ConversationMessage(id=3, role="user",      content="再查一下销售部"),
    #         ConversationMessage(id=4, role="assistant",  content="SELECT ... FROM sales..."),
    #     ]
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
