"""Conversation history ORM entities and API schemas."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.config.database import Base


class Conversation(Base):
    """A chat conversation owned by one user."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    data_source: Mapped[str] = mapped_column(String(64), default="sqlite_demo", nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    last_sql: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation",  # 双向绑定：消息那边也有 conversation 字段指回会话
        cascade="all, delete-orphan",  # 删除会话时，自动删除它下面所有消息
        passive_deletes=True,  # 删除时依赖数据库的外键级联，不额外发 SQL
        lazy="selectin",  # 查询会话时自动用额外一条 SQL 把消息一起加载
    )


class ConversationMessage(Base):
    """One user or AI message in a conversation."""

    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sql_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    columns_json: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    results_json: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    execution_time: Mapped[Optional[float]] = mapped_column(nullable=True)
    insight: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ConversationCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(..., min_length=1, max_length=255)
    data_source: str = Field(default="sqlite_demo", max_length=64)
    model_id: str = Field(..., min_length=1, max_length=128)
    model_conf: Optional[dict[str, Any]] = Field(default=None, alias="model_config")


class ConversationSummary(BaseModel):
    id: str
    title: str
    data_source: str
    model_id: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationMessageOut(BaseModel):
    id: str
    role: str
    content: str
    sql: Optional[str] = None
    columns: Optional[list[Any]] = None
    results: Optional[list[dict[str, Any]]] = None
    row_count: Optional[int] = None
    execution_time: Optional[float] = None
    insight: Optional[str] = None
    success: Optional[bool] = None
    error: Optional[str] = None
    created_at: datetime


class ConversationDetail(ConversationSummary):
    model_config = ConfigDict(populate_by_name=True)

    model_conf: Optional[dict[str, Any]] = Field(default=None, alias="model_config")
    last_sql: Optional[str] = None
    messages: list[ConversationMessageOut] = []
