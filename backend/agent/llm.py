"""OpenAI-compatible Qwen clients for controlled planning, review and repair."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from backend.agent.contracts import AgentPlan, MetadataContext, ReviewerDecision, SourceCandidate, SqlRepairProposal, SqlReviewDecision
from backend.config.config import settings
from backend.nl2sql.prompt_builder import PromptBuilder


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


def _plan_join_edges(plan: AgentPlan, contexts: list[MetadataContext]) -> list[dict[str, str]]:
    """Return server-verified FK edges between objects requested by a plan."""
    contexts_by_source = {context.source.source_id: context for context in contexts}
    edges: set[tuple[str, str, str, str, str]] = set()
    for task in plan.subtasks:
        context = contexts_by_source.get(task.source_id)
        required_objects = set(task.object_ids)
        if context is None or len(required_objects) < 2:
            continue
        for table in context.tables:
            table_name = str(table.get("name") or "")
            if table_name not in required_objects:
                continue
            for foreign_key in table.get("foreign_keys") or []:
                column = str(foreign_key.get("column") or "")
                ref_table = str(foreign_key.get("ref_table") or "")
                ref_column = str(foreign_key.get("ref_column") or "")
                if column and ref_table in required_objects and ref_column:
                    edges.add((task.source_id, table_name, column, ref_table, ref_column))
    return [
        {
            "source_id": source_id,
            "from_table": from_table,
            "from_column": from_column,
            "to_table": to_table,
            "to_column": to_column,
        }
        for source_id, from_table, from_column, to_table, to_column in sorted(edges)
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
    """调用 OpenAI 兼容接口，并安全地返回模型生成的 JSON 对象。

    这个函数只负责“通信”和“基础格式解析”，不负责判断计划是否安全、
    SQL 是否正确。计划边界由后续的程序校验节点负责。

    参数说明：
    - ``base_url``：模型服务地址，例如 DashScope 的兼容接口地址。
    - ``api_key``：接口密钥；为空时不添加 Authorization 请求头。
    - ``model``：要调用的模型名称。
    - ``system_prompt``：模型的角色和行为规则。
    - ``user_payload``：发送给模型的结构化业务输入，会被序列化为 JSON 字符串。
    - ``max_tokens``：限制模型最多生成多少 token。
    - ``timeout``：单次 HTTP 请求的超时时间。
    - ``disable_thinking``：部分兼容服务支持关闭隐藏思考，以便快速返回 JSON。
    - ``transient_retries``：网络超时或网络错误时，最多额外重试多少次。

    返回值：模型返回的 JSON 对象（Python ``dict``）。
    任何通信失败、响应结构不对或 JSON 无法解析，都会转换成 AgentLLMError。
    """
    # 先准备 HTTP 请求头。没有 API Key 时不发送空的 Bearer，避免形成无效鉴权。
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # 按 OpenAI Chat Completions 格式组装请求体：
    # - system message：告诉模型它的角色和规则；
    # - user message：放入本次问题、候选数据源、Schema 等结构化输入；
    # - temperature=0：尽量减少同一输入下的随机变化；
    # - response_format：要求服务返回 JSON；
    # - stream=False：本函数一次性等待完整响应，不使用流式输出。
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
        # 某些 OpenAI 兼容服务支持这个参数。
        # 审核结果只需要简短、确定的 JSON，不需要额外的隐藏思考过程。
        payload["thinking"] = {"type": "disabled"}

    # transient_retries=0 时只循环一次；设置为 1 时最多尝试两次。
    for attempt in range(transient_retries + 1):
        try:
            # 每次尝试都创建一个带超时限制的异步 HTTP 客户端。
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)

            # HTTP 4xx/5xx 会在这里转换成 httpx.HTTPStatusError，进入下面的统一错误处理。
            response.raise_for_status()

            # 按 OpenAI 兼容响应结构提取文本：
            # response.json()["choices"][0]["message"]["content"]
            content = response.json()["choices"][0]["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                # 空内容或非字符串内容都不能交给后续计划/审核逻辑。
                raise ValueError("empty model content")

            # 模型返回的是 JSON 文本，这里把它解析成 Python 字典。
            parsed = json.loads(content)
            break
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # 超时和网络中断属于暂时性错误，可以按配置重试。
            if attempt < transient_retries:
                # 第一次重试等待 1 秒，第二次等待 2 秒，避免立即连续冲击服务。
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            # 对外只返回安全错误码，不泄露 URL、响应正文、Prompt 或密钥。
            raise AgentLLMError(_safe_llm_error_code(exc)) from exc
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # HTTP 协议错误、响应字段缺失、类型错误、空内容或非法 JSON，
            # 都不是适合盲目重试的错误，直接转换为安全的 AgentLLMError。
            raise AgentLLMError(_safe_llm_error_code(exc)) from exc

    # 即使服务声称返回 JSON，也要再次确认顶层类型是对象。
    # 后续 QwenPlanner 等代码依赖 dict 结构，数组、字符串、数字都必须拒绝。
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
            model=settings.agent_planner_effective_model,
            timeout=settings.llm_timeout,
            max_tokens=settings.agent_planner_max_tokens,
            system_prompt=(
                "你是受控 NL2SQL 规划器。必须只输出一个 JSON 对象。只能选择输入 candidates/context 中的 "
                "source_id、object_ids 和字段；不能输出 SQL、连接信息、URL 或写操作。所有当前来源（包括 REST 和 GraphQL）"
                "都通过后端固定端点映射成受控只读虚拟表，因此 operation_id 必须为 readonly_sql；绝不能使用 rest_get 或 graphql_query。"
                "你只能使用服务器已经执行并返回的 planning_tools 结果；不能请求其他数据源、全量 Schema、数据库连接或执行工具。"
                "For a normal one-source question, return route_mode exactly 'single_source', exactly one subtask, an ASCII lowercase "
                "subtask id using only letters/digits/underscores, operation_id exactly 'readonly_sql', merge_contract_id as JSON null, "
                "and confidence as a number from 0 to 1. Do not wrap JSON in Markdown and do not add fields outside the requested schema."
                " When revision_reasons is non-empty, explain in concise Chinese how this plan addresses them. "
                "For a self-referencing table, a second output alias such as managers.name is allowed when the metadata has a self foreign key."
            ),
            user_payload={
                "question": question,
                "source_candidates": [candidate.model_dump() for candidate in candidates],
                "metadata_contexts": _context_payload(contexts),
                "planning_tools": _planning_tool_results(candidates, contexts),
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
                    "revision_summary_zh": "仅在修订计划时，用中文简要说明针对原因做了什么调整；首次计划填空字符串",
                },
            },
        )
        try:
            return AgentPlan.model_validate(result)
        except ValueError as exc:
            raise AgentLLMError(f"Qwen 规划 JSON 不符合 AgentPlan 契约:{_plan_contract_error_summary(exc)}") from exc


class QwenPlanReviewer:
    """Separately prompted Qwen review of a validated plan; it never executes or edits SQL."""

    async def review(
        self,
        question: str,                              # 用户原始问句
        plan: AgentPlan,                            # 规划器刚生成的执行计划
        contexts: list[MetadataContext],            # 服务端提供的元数据（表/字段等）
    ) -> ReviewerDecision:
        join_edges = _plan_join_edges(plan, contexts)
        # 第一步：让 Qwen 当"复审员"，审查这个 plan 合不合格
        result = await _request_json(
            base_url=settings.dashscope_base_url,
            api_key=settings.dashscope_api_key,
            model=settings.agent_planner_effective_model,
            timeout=settings.llm_timeout,
            max_tokens=400,
            # 系统提示：明确告诉 Qwen 这是"计划阶段"审查，不是 SQL 审查
            # 几个关键约束：
            #   1) 计划阶段还没生成 SQL，筛选语义写在 goal 里是允许的
            #   2) 除非 goal 真的和原问题冲突/遗漏，否则别瞎报 QUESTION_SCOPE_CHANGED
            #   3) Qwen 只能审、不能自己写 SQL、不能自己跑工具（防止越权）
            #   4) 严格只输出 JSON
            system_prompt=(
                "This is plan-stage review, not SQL review. A task goal can carry filter semantics before SQL exists. "
                "Do not use QUESTION_SCOPE_CHANGED when the goal restates the question; use it only for a direct conflict or omission. "
                "join_edges are server-verified foreign-key relationships between the plan's requested tables. "
                "When join_edges is non-empty, do not return MISSING_JOIN_KEY for that plan. "
                "When revising or rejecting, reason_summary_zh must be a concise Chinese explanation for the user. "
                "你是受控 NL2SQL 计划审查者，与规划步骤角色隔离。只输出 JSON。审核计划是否忠实回答问题、是否只使用 "
                "服务器提供的 metadata 与 planning_tools、是否存在不可靠跨源融合或越权操作。你不能生成 SQL、不能执行工具。"
            ),
            # 用户负载：把问题、plan、元数据、可用的规划工具一起喂给 Qwen
            # 这里 planning_tools 先传空列表，等后续需要再补
            # required_json_schema 强制 Qwen 按指定 JSON 结构返回，方便后续解析
            user_payload={
                "question": question,
                "plan": plan.model_dump(),
                "metadata_contexts": _context_payload(contexts),
                "planning_tools": _planning_tool_results([], contexts),
                "join_edges": join_edges,
                "required_json_schema": {
                    "decision": "approve | revise | reject",   # 三种决策：放行 / 打回重做 / 拒绝
                    "reason_codes": [                          # 问题原因码
                        "MISSING_JOIN_KEY | UNAUTHORIZED_SOURCE | "
                        "UNSUPPORTED_FUSION_KEY | QUESTION_SCOPE_CHANGED"
                    ],
                    "reason_summary_zh": "中文简要说明；approve 时填空字符串",
                },
            },
        )

        # 第二步：把 Qwen 的 JSON 结果校验成 ReviewerDecision 对象
        # 格式不对就抛 AgentLLMError——说明 Qwen 输出坏了，得修
        try:
            decision = ReviewerDecision.model_validate(result)
        except ValueError as exc:
            raise AgentLLMError("Qwen 计划审核 JSON 不符合 ReviewerDecision 契约") from exc

        # Do not block a valid plan when the model's only objection is a join
        # key that the server has already proved through trusted metadata.
        if join_edges and set(decision.reason_codes) == {"MISSING_JOIN_KEY"}:
            return ReviewerDecision(decision="approve", reason_codes=[])

        # 第三步：兜底纠错，纠正 Qwen 在"范围"问题上的常见误报
        # scope_only：Qwen 提的所有问题里只有"范围变了"这一种（没有别的硬伤）
        scope_only = set(decision.reason_codes) <= {"QUESTION_SCOPE_CHANGED"}
        # 兜底1：如果 Qwen 只提了"范围变了"这一种意见，
        # 并且 plan 的 goal 其实是原样复述了用户原问题，
        # 那这就是个假阳性——计划阶段筛选条件本来就在 goal 里，
        # 真正的 SQL 过滤校验放在 XiYan 生成 SQL 之后那一道关卡
        if scope_only and _plan_goal_exactly_preserves_question(question, plan):
            return ReviewerDecision(decision="approve", reason_codes=[])
        # 兜底2：如果 Qwen 给了 reject，但理由只有"范围变了"这一条，
        # 降级成 revise——给规划器一次返工机会，不要直接判死刑
        if decision.decision == "reject" and scope_only:
            return ReviewerDecision(decision="revise", reason_codes=decision.reason_codes)
        # 其他情况：原样返回 Qwen 的判定
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
            model=settings.agent_planner_effective_model,
            timeout=settings.llm_timeout,
            max_tokens=settings.agent_planner_max_tokens,
            system_prompt=(
                "你是受控 SQL 故障诊断者。只输出 JSON。不得放宽原问题的时间、城市、状态等筛选条件；"
                "只可提出一条 SELECT/WITH 修复 SQL，且必须使用给定 schema。"
                "当 error_message 为 MONTH_TIME_RANGE_REQUIRED:YYYY-MM 时，必须使用该月首日（含）到下月首日（不含）的明确半开时间区间。"
                "当 error_message 为 LATEST_PRICE_MUST_NOT_FILTER_HISTORICAL_DATE 时，使用 v_stock_latest_price 时不得再以固定历史日期筛选 trade_date。"
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


class QwenSqlReviewer:
    """Separately prompted Qwen review of one repair candidate before retry."""

    async def review(
        self,
        *,
        question: str,
        failed_sql: str,
        proposal: SqlRepairProposal,
        context: MetadataContext,
    ) -> SqlReviewDecision:
        result = await _request_json(
            base_url=settings.dashscope_base_url,
            api_key=settings.dashscope_api_key,
            model=settings.agent_planner_effective_model,
            timeout=settings.llm_timeout,
            max_tokens=300,
            system_prompt=(
                "你是 SQL 修复候选的审查者，与诊断步骤角色隔离。只输出 JSON。只有在候选保持原问题过滤条件、"
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
            raise AgentLLMError("Qwen SQL 审核 JSON 不符合契约") from exc


def _planning_tool_results(candidates: list[SourceCandidate], contexts: list[MetadataContext]) -> list[dict[str, Any]]:
    """Expose retrieve output as immutable, server-resolved planning tools.

    They are intentionally not database tools: Qwen can inspect only the
    candidate sources, Schema closure, and matching template profile already
    selected by retrieve. It cannot expand scope or execute SQL.
    """
    prompt_builder = PromptBuilder()
    return [
        {"name": "list_retrieved_sources", "result": [item.model_dump() for item in candidates]},
        {"name": "get_retrieved_schema", "result": _context_payload(contexts)},
        {
            "name": "get_source_template_profile",
            "result": [prompt_builder.get_agent_source_template(item.source.source_id) for item in contexts],
        },
    ]
