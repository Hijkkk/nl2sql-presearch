"""
定义 Agent 计划、子任务、元数据上下文和 XiYan Prompt 上下文的 Pydantic 契约。
作用是让 Qwen 后续只能输出受限 JSON，而非自由调用数据库。
验证数据格式
类型提示
API 请求/响应的序列化/反序列化
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceDescriptor(BaseModel):
    """描述一个用户已授权的数据源。"""

    source_id: str
    source_type: str
    dialect: str
    description: str
    capabilities: list[str] = Field(default_factory=list)


class SourceCandidate(SourceDescriptor):
    # 在数据源检索时，给每个候选打分。继承SourceDescriptor
    score: float
    matched_terms: list[str] = Field(default_factory=list)
    retrieval_method: str = "keyword"
    # Keep the score components for traceability.  The planner still receives
    # only the bounded candidate list, while operators can see why it ranked.
    lexical_score: float = 0.0
    semantic_score: float | None = None
    hybrid_score: float | None = None


class MetadataContext(BaseModel):
    """传递给 SQL 生成器的完整上下文信息。"""

    source: SourceDescriptor  # 数据源描述
    selected_object_ids: list[str]  # 用户初步选中的表
    schema_closure_object_ids: list[str]  # 补全后的表
    schema_signature: str = ""  # Schema 签名（用于缓存）
    tables: list[dict]  # 表的详细元数据
    lexical_selected_object_ids: list[str] = Field(default_factory=list)
    semantic_object_hits: list[dict] = Field(default_factory=list)


class XiYanPromptContext(BaseModel):
    """用于构建 XiYan 模型的 Prompt，包含严格的验证规则。"""

    source_id: str                                # 数据源 ID
    dialect: str                                  # SQL 方言
    schema_signature: str = ""                    # Schema 签名
    question: str = Field(min_length=1, max_length=500)  # 用户问题（1-500字符）
    task_goal: str = Field(default="", max_length=500)
    required_object_ids: list[str] = Field(default_factory=list)
    planned_output_fields: list[str] = Field(default_factory=list)
    schema_closure_object_ids: list[str] = Field(min_length=1, max_length=3)  # 表名列表（1-3个）
    allowed_field_ids: list[str] = Field(default_factory=list)  # 允许的字段
    max_rows: int = Field(default=1000, ge=1, le=10000)  # 最大行数（1-10000）


class AgentSubtask(BaseModel):
    # 定义多步骤任务中的一个子任务。
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")  # ID（小写字母+数字+下划线）
    source_id: str                                      # 数据源 ID
    operation_id: Literal["readonly_sql", "rest_get", "graphql_query"]  # 操作类型
    goal: str = Field(min_length=1, max_length=500)     # 目标描述
    object_ids: list[str] = Field(default_factory=list) # 涉及的表
    output_fields: list[str] = Field(default_factory=list)  # 输出字段
    depends_on: list[str] = Field(default_factory=list)   # 依赖的子任务 ID


class AgentPlan(BaseModel):
    """返回计划验证的结果。"""

    route_mode: Literal["single_source", "multi_source"]
    subtasks: list[AgentSubtask] = Field(min_length=1, max_length=5)
    merge_contract_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    revision_summary_zh: str = Field(default="", max_length=200)


class PlanValidationResult(BaseModel):
    status: Literal["approved", "revise", "rejected"]
    reason_codes: list[str] = Field(default_factory=list)
    reason_summary_zh: str = Field(default="", max_length=300)


class ReviewerDecision(BaseModel):
    decision: Literal["approve", "revise", "reject"]
    reason_codes: list[str] = Field(default_factory=list)
    reason_summary_zh: str = Field(default="", max_length=300)


class SqlRepairProposal(BaseModel):
    diagnosis: str = Field(min_length=1, max_length=1000)
    can_retry: bool
    proposed_sql: str = ""
    risk: Literal["low", "medium", "high"]


class SqlReviewDecision(BaseModel):
    approve: bool
    reason_codes: list[str] = Field(default_factory=list)


class AgentExecutionResult(BaseModel):
    success: bool
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    results: list[dict] = Field(default_factory=list)
    row_count: int = 0
    retry_attempted: bool = False
    error: str | None = None
    # Server-side generation evidence for the trace; never contains credentials
    # or result rows.
    generation_trace: dict = Field(default_factory=dict)
    # Records bounded server-side repair decisions without exposing credentials
    # or raw database rows.  This is separate from the XiYan generation trace so
    # operators can distinguish the original candidate from the final SQL.
    repair_trace: dict = Field(default_factory=dict)
