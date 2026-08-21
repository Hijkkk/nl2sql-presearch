"""Async CRUD functions for conversation history."""
from datetime import datetime, timedelta
import json
from typing import Optional
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.config.config import settings
from backend.models.conversation import (
    Conversation,
    ConversationCreate,
    ConversationDetail,
    ConversationMessage,
    ConversationMessageOut,
    ConversationSummary,
)


_AGENT_PRESENTATION_PREFIX = "__NL2SQL_AGENT_PRESENTATION__:"
_AGENT_HISTORY_MAX_BYTES = 48 * 1024
_AGENT_TRACE_ONLY_KEYS = frozenset({
    # These fields are intentionally retained in data/agent_traces only.  They
    # can include a full prompt plus the raw model response and easily exceed
    # MySQL TEXT's 64 KiB limit when embedded in a conversation message.
    "generation",
    "generation_trace",
    "prompt_template",
    "raw_model_output",
})


def _history_safe_agent_presentation(presentation: dict) -> dict:
    """Return the compact Agent payload used by durable chat history.

    The trace JSON is the audit source of truth.  Conversation history only
    needs enough information to restore the Agent cards in the UI, so it must
    never contain the full generated prompt or raw LLM output.
    """
    def compact(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in _AGENT_TRACE_ONLY_KEYS:
                    result[key] = "[仅保存在 Agent JSON 审计轨迹中]"
                else:
                    result[key] = compact(item)
            return result
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    compacted = compact(presentation)
    encoded = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= _AGENT_HISTORY_MAX_BYTES:
        return compacted

    # A pathological retrieve/plan result can still be large.  Preserve the
    # final decision and timings, but reduce event details rather than risking
    # a failed user request merely because history is verbose.
    events = compacted.get("events")
    if isinstance(events, list):
        compacted["events"] = [
            {
                key: event.get(key)
                for key in ("node", "status", "reason", "duration_ms")
                if key in event
            }
            for event in events
            if isinstance(event, dict)
        ]
        compacted["history_details_compacted"] = True
    encoded = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= _AGENT_HISTORY_MAX_BYTES:
        return compacted

    # Last-resort guarantee for unusually large plans/schemas.  A history
    # write must never turn an otherwise successful query into HTTP 500.
    execution = compacted.get("execution")
    return {
        "is_agent": True,
        "status": compacted.get("status"),
        "execution": {
            key: execution.get(key)
            for key in ("success", "row_count", "retry_attempted", "error")
            if isinstance(execution, dict) and key in execution
        },
        "error": compacted.get("error"),
        "execution_time": compacted.get("execution_time"),
        "stage_timings": compacted.get("stage_timings", {}),
        "history_details_compacted": True,
        "trace_hint": "完整计划、审核和 Prompt 请查看 Agent JSON 审计轨迹。",
    }


def _decode_agent_presentation(value: str | None) -> dict | None:
    if not value or not value.startswith(_AGENT_PRESENTATION_PREFIX):
        return None
    try:
        decoded = json.loads(value[len(_AGENT_PRESENTATION_PREFIX):])
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


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
    show_debug = settings.nl2sql_debug_output
    agent_data = _decode_agent_presentation(message.insight)
    return ConversationMessageOut(
        id=message.id,
        role=message.role,
        content=message.content,
        sql=message.sql_text if show_debug else None,
        columns=message.columns_json,
        results=message.results_json,
        row_count=message.row_count,
        # 耗时属于请求状态，不是 SQL / 模型解释等调试内容。历史会话必须始终返回它，
        # 否则 NL2SQL_DEBUG_OUTPUT=false 时前端重新打开会话会显示“耗时：-”。
        execution_time=message.execution_time,
        insight=message.insight if show_debug and not agent_data else None,
        success=message.success,
        error=message.error_message if show_debug else None,
        agent_data=agent_data,
        created_at=message.created_at,
    )


def to_detail(conversation: Conversation) -> ConversationDetail:
    # SQLite 的 CURRENT_TIMESTAMP 精度只有秒；同一轮问答相同时间时，用户消息必须在 AI 消息前。
    messages = sorted(
        conversation.messages,
        key=lambda item: (item.created_at, 0 if item.role == "user" else 1),
    )
    return ConversationDetail(
        **to_summary(conversation).model_dump(),
        model_conf=conversation.model_config,
        last_sql=conversation.last_sql if settings.nl2sql_debug_output else None,
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
    search: str = "",
) -> tuple[list[ConversationSummary], int]:
    # 防止前端传恶意参数（如 page=-999、page_size=100000）导致数据库压力过大或返回异常数据。
    # 防止页码 ≤ 0，最小取 1
    page = max(page, 1)
    # 防止每页数量 ≤ 0，最小取 1，最大取 100
    page_size = min(max(page_size, 1), 100)
    # 计算分页偏移量
    offset = (page - 1) * page_size
    keyword = search.strip()
    filters = [Conversation.user_id == user_id]
    if keyword:
        filters.append(Conversation.title.ilike(f"%{keyword}%"))
    # 查询总数
    total_stmt = select(func.count()).select_from(Conversation).where(*filters)
    # 执行查询
    total = await db.scalar(total_stmt) or 0

    # 查询分页数据
    # 优先按 updated_at 降序：最近有更新的对话排在最前面（比如刚发了新消息的会话）
    # 相同则按 created_at 降序：如果更新时间一样，新建的排前面
    stmt = (
        select(Conversation)
        .where(*filters)
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


async def rename_conversation(
    db: AsyncSession,
    user_id: int,
    conversation_id: str,
    title: str,
) -> Optional[ConversationSummary]:
    conversation = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    if not conversation:
        return None
    conversation.title = title.strip()
    await db.commit()
    await db.refresh(conversation)
    return to_summary(conversation)


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
    presentation: Optional[dict] = None,
) -> tuple[str, str]:
    """Persist one user question and one AI answer into conversation history."""
    history_presentation = (
        _history_safe_agent_presentation(presentation)
        if presentation is not None
        else None
    )
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
        created_at=datetime.now(),
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
        insight=(
            _AGENT_PRESENTATION_PREFIX + json.dumps(history_presentation, ensure_ascii=False, separators=(",", ":"))
            if history_presentation is not None
            else insight
        ),
        success=success,
        error_message=error,
        created_at=datetime.now() + timedelta(microseconds=1),
    )
    db.add_all([user_message, ai_message])

    conversation.last_sql = sql
    conversation.message_count = conversation.message_count + 2
    conversation.data_source = data_source
    conversation.model_id = model_id or conversation.model_id
    conversation.model_config = model_config or conversation.model_config

    await db.commit()
    return conversation.id, ai_message.id
