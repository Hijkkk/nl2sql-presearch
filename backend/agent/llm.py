"""OpenAI-compatible Qwen planner and DeepSeek reviewer clients."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from backend.agent.contracts import AgentPlan, MetadataContext, ReviewerDecision, SourceCandidate, SqlRepairProposal, SqlReviewDecision
from backend.config.config import settings


class AgentLLMError(RuntimeError):
    """Raised when a planner/reviewer response cannot be safely consumed."""


def _normalise_scope_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def _plan_goal_exactly_preserves_question(question: str, plan: AgentPlan) -> bool:
    """A plan-stage reviewer cannot require SQL filters that do not exist yet."""
    normalized_question = _normalise_scope_text(question)
    return bool(normalized_question) and any(
        _normalise_scope_text(task.goal) == normalized_question
        for task in plan.subtasks
    )


def _safe_llm_error_code(exc: Exception) -> str:
    """Expose an operational code without response bodies, keys, or prompts."""
    if isinstance(exc, httpx.TimeoutException):
        return "LLM_TIMEOUT"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"LLM_HTTP_{exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "LLM_NETWORK_ERROR"
    if isinstance(exc, json.JSONDecodeError):
        return "LLM_INVALID_JSON"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "LLM_INVALID_RESPONSE"
    return "LLM_UNKNOWN_ERROR"


def _plan_contract_error_summary(exc: ValueError) -> str:
    """Return schema-only Pydantic errors; never include model content or secrets."""
    errors = getattr(exc, "errors", lambda: [])()
    details = []
    for error in errors[:6]:
        location = ".".join(str(part) for part in error.get("loc", ("unknown",)))
        error_type = str(error.get("type", "invalid"))
        details.append(f"{location}:{error_type}")
    return ";".join(details) or "invalid_structure"


def _context_payload(contexts: list[MetadataContext]) -> list[dict[str, Any]]:
    return [
        {
            "source": context.source.model_dump(),
            "schema_signature": context.schema_signature,
            "selected_object_ids": context.selected_object_ids,
            "schema_closure_object_ids": context.schema_closure_object_ids,
            "tables": context.tables,
        }
        for context in contexts
    ]


async def _request_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    max_tokens: int,
    timeout: float,
    disable_thinking: bool = False,
    transient_retries: int = 0,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    if disable_thinking:
        # DeepSeek V4 defaults to thinking mode.  The reviewer needs a short,
        # deterministic JSON verdict rather than hidden reasoning content.
        payload["thinking"] = {"type": "disabled"}
    for attempt in range(transient_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty model content")
            parsed = json.loads(content)
            break
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt < transient_retries:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            raise AgentLLMError(_safe_llm_error_code(exc)) from exc
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentLLMError(_safe_llm_error_code(exc)) from exc
    if not isinstance(parsed, dict):
        raise AgentLLMError("受控 Agent 模型未返回 JSON 对象")
    return parsed


class QwenPlanner:
    """Produces an untrusted structured plan; policy validation is mandatory."""

    async def plan(
        self,
        question: str,
        candidates: list[SourceCandidate],
        contexts: list[MetadataContext],
        revision_reasons: list[str] | None = None,
    ) -> AgentPlan:
        if not settings.agent_planner_dashscope_enabled or not settings.dashscope_api_key:
            raise AgentLLMError("Qwen 规划器未启用或未配置 DASHSCOPE_API_KEY")
        result = await _request_json(
            base_url=settings.dashscope_base_url,
            api_key=settings.dashscope_api_key,
            model=settings.agent_planner_model,
            timeout=settings.llm_timeout,
            max_tokens=settings.agent_planner_max_tokens,
            system_prompt=(
                "你是受控 NL2SQL 规划器。必须只输出一个 JSON 对象。只能选择输入 candidates/context 中的 "
                "source_id、object_ids 和字段；不能输出 SQL、连接信息、URL 或写操作。所有当前来源（包括 REST 和 GraphQL）"
                "都通过后端固定端点映射成受控只读虚拟表，因此 operation_id 必须为 readonly_sql；绝不能使用 rest_get 或 graphql_query。"
                "For a normal one-source question, return route_mode exactly 'single_source', exactly one subtask, an ASCII lowercase "
                "subtask id using only letters/digits/underscores, operation_id exactly 'readonly_sql', merge_contract_id as JSON null, "
                "and confidence as a number from 0 to 1. Do not wrap JSON in Markdown and do not add fields outside the requested schema."
            ),
            user_payload={
                "question": question,
                "source_candidates": [candidate.model_dump() for candidate in candidates],
                "metadata_contexts": _context_payload(contexts),
                "revision_reasons": revision_reasons or [],
                "required_json_schema": {
                    "route_mode": "single_source | multi_source",
                    "subtasks": [{
                        "id": "department_sales", "source_id": "sqlite_demo",
                        "operation_id": "readonly_sql", "goal": "按部门统计销售总额",
                        "object_ids": ["sales", "departments"], "output_fields": ["sales.amount", "departments.name"], "depends_on": [],
                    }],
                    "merge_contract_id": "For single_source use JSON null, not the string 'null'. For multi_source use city_key_left_join, date_key_left_join, or code_key_left_join.",
                    "confidence": 0.0,
                },
            },
        )
        try:
            return AgentPlan.model_validate(result)
        except ValueError as exc:
            raise AgentLLMError(f"Qwen 规划 JSON 不符合 AgentPlan 契约:{_plan_contract_error_summary(exc)}") from exc


class DeepSeekReviewer:
    """Independently reviews a validated plan; it never executes or edits SQL."""

    async def review(
        self,
        question: str,
        plan: AgentPlan,
        contexts: list[MetadataContext],
    ) -> ReviewerDecision:
        if not settings.deepseek_reviewer_enabled or not settings.deepseek_api_key:
            raise AgentLLMError("DeepSeek 审核未启用或未配置 DEEPSEEK_API_KEY")
        result = await _request_json(
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            timeout=settings.deepseek_timeout,
            max_tokens=400,
            disable_thinking=True,
            transient_retries=settings.deepseek_reviewer_retries,
            system_prompt=(
                "This is plan-stage review, not SQL review. A task goal can carry filter semantics before SQL exists. "
                "Do not use QUESTION_SCOPE_CHANGED when the goal restates the question; use it only for a direct conflict or omission. "
                "你是受控 NL2SQL 计划的独立审查者。只输出 JSON。审核计划是否忠实回答问题、是否只使用 "
                "给定 metadata、是否存在不可靠跨源融合或越权操作。你不能生成 SQL、不能执行工具。"
            ),
            user_payload={
                "question": question,
                "plan": plan.model_dump(),
                "metadata_contexts": _context_payload(contexts),
                "required_json_schema": {
                    "decision": "approve | revise | reject",
                    "reason_codes": ["MISSING_JOIN_KEY | UNAUTHORIZED_SOURCE | UNSUPPORTED_FUSION_KEY | QUESTION_SCOPE_CHANGED"],
                },
            },
        )
        try:
            decision = ReviewerDecision.model_validate(result)
        except ValueError as exc:
            raise AgentLLMError("DeepSeek 审核 JSON 不符合 ReviewerDecision 契约") from exc
        scope_only = set(decision.reason_codes) <= {"QUESTION_SCOPE_CHANGED"}
        # At plan stage, the goal is the only representation of filters.  If it
        # exactly restates the question, a scope-only objection is a reviewer
        # false positive; SQL filters are guarded after XiYan generation.
        if scope_only and _plan_goal_exactly_preserves_question(question, plan):
            return ReviewerDecision(decision="approve", reason_codes=[])
        # A scope concern is otherwise recoverable: route it through the one
        # permitted Qwen revision instead of treating it as unsafe operation.
        if decision.decision == "reject" and scope_only:
            return ReviewerDecision(decision="revise", reason_codes=decision.reason_codes)
        return decision


class QwenSqlDiagnostician:
    """Creates one untrusted repair candidate after a failed single-source SQL."""

    async def diagnose(
        self,
        *,
        question: str,
        failed_sql: str,
        error_message: str,
        context: MetadataContext,
    ) -> SqlRepairProposal:
        result = await _request_json(
            base_url=settings.dashscope_base_url,
            api_key=settings.dashscope_api_key,
            model=settings.agent_planner_model,
            timeout=settings.llm_timeout,
            max_tokens=settings.agent_planner_max_tokens,
            system_prompt=(
                "你是受控 SQL 故障诊断者。只输出 JSON。不得放宽原问题的时间、城市、状态等筛选条件；"
                "只可提出一条 SELECT/WITH 修复 SQL，且必须使用给定 schema。"
                "当 error_message 为 MONTH_TIME_RANGE_REQUIRED:YYYY-MM 时，必须使用该月首日（含）到下月首日（不含）的明确半开时间区间。"
            ),
            user_payload={
                "question": question,
                "failed_sql": failed_sql,
                "error_message": error_message,
                "metadata_context": _context_payload([context])[0],
                "required_json_schema": {
                    "diagnosis": "brief explanation", "can_retry": True,
                    "proposed_sql": "SELECT ...", "risk": "low | medium | high",
                },
            },
        )
        try:
            return SqlRepairProposal.model_validate(result)
        except ValueError as exc:
            raise AgentLLMError("Qwen SQL 修复 JSON 不符合契约") from exc


class DeepSeekSqlReviewer:
    """Reviews a Qwen repair candidate before the one permitted retry."""

    async def review(
        self,
        *,
        question: str,
        failed_sql: str,
        proposal: SqlRepairProposal,
        context: MetadataContext,
    ) -> SqlReviewDecision:
        result = await _request_json(
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            timeout=settings.deepseek_timeout,
            max_tokens=300,
            disable_thinking=True,
            transient_retries=settings.deepseek_reviewer_retries,
            system_prompt=(
                "你是 SQL 修复候选的独立审查者。只输出 JSON。只有在候选保持原问题过滤条件、"
                "只读且只使用给定 schema 时才能 approve；你不能修改 SQL 或执行工具。"
            ),
            user_payload={
                "question": question,
                "failed_sql": failed_sql,
                "proposal": proposal.model_dump(),
                "metadata_context": _context_payload([context])[0],
                "required_json_schema": {"approve": True, "reason_codes": ["FILTER_WIDENED | FIELD_OUTSIDE_SCHEMA | UNSAFE_SQL"]},
            },
        )
        try:
            return SqlReviewDecision.model_validate(result)
        except ValueError as exc:
            raise AgentLLMError("DeepSeek SQL 审核 JSON 不符合契约") from exc
