"""
LangGraph 编排，用于仅记录的受控代理部署。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agent.contracts import AgentExecutionResult, AgentPlan, MetadataContext, PlanValidationResult, ReviewerDecision, SourceCandidate
from backend.agent.execution import ControlledSingleSourceExecutor
from backend.agent.llm import AgentLLMError, QwenPlanReviewer, QwenPlanner
from backend.agent.policy import validate_plan
from backend.agent.service import AgentPreparationService
from backend.agent.tools import extract_chinese_bigrams, tokenize_question
from backend.config.audit import log_agent_trace, write_agent_execution_trace
from backend.config.config import settings


_REASON_SUMMARIES_ZH = {
    "MISSING_JOIN_KEY": "计划涉及多表但未确认可用关联键；请使用 Schema 中明确的外键关系。",
    "QUESTION_SCOPE_CHANGED": "计划的目标、筛选条件或输出范围与原问题不一致，需要按原问题修订。",
    "UNAUTHORIZED_SOURCE": "计划使用了未被检索阶段授权的数据源。",
    "UNSUPPORTED_FUSION_KEY": "跨数据源融合缺少系统支持的统一关联键。",
    "FIELD_OUTSIDE_SCHEMA_CLOSURE": "计划引用了最终 Schema 中不存在的表别名或字段；请改为允许的物理字段，或使用已确认自联结表的别名。",
    "OBJECT_OUTSIDE_SCHEMA_CLOSURE": "计划使用的表不在本次最终 Schema 范围内。",
}


def _reason_summary_zh(reason_codes: list[str]) -> str:
    """Provide an operator-readable Chinese explanation for deterministic checks."""
    summaries = []
    for code in reason_codes:
        prefix = str(code).split(":", 1)[0]
        summaries.append(_REASON_SUMMARIES_ZH.get(prefix, f"计划需要修订：{code}"))
    return "；".join(dict.fromkeys(summaries))

# "任何拥有 plan() 方法的类，都可以被当作 Planner 使用"。
# 这是一种结构化子类型（structural subtyping），也叫鸭子类型的静态版本。
# 运行时：Python 不检查 Protocol，只要对象有 plan 方法就能调用
# 开发时：mypy/pyright 会检查 QwenPlanner 是否真的实现了 plan 方法
class Planner(Protocol):
    async def plan(self, question: str, candidates: list[SourceCandidate], contexts: list[MetadataContext], revision_reasons: list[str] | None = None) -> AgentPlan: ...


class Reviewer(Protocol):
    async def review(self, question: str, plan: AgentPlan, contexts: list[MetadataContext]) -> ReviewerDecision: ...


class Summarizer(Protocol):
    async def summarize_result(self, question: str, columns: list[str], results: list[dict], **kwargs: Any) -> str: ...

# 1. 状态定义（AgentGraphState）
# 图中每个节点都读取同一份 state，然后只返回需要更新的部分。
class AgentGraphState(TypedDict, total=False):
    """
    这个状态就像一份"病历表"，从开始到结束，每个医生（节点）都会在上面记录信息。
    """
    request_id: str          # 本次请求的唯一ID
    question: str            # 用户的自然语言问题
    source_hint: str         # 数据源提示
    candidates: list[SourceCandidate]  # 找到的候选数据源
    contexts: list[MetadataContext]  # 表结构/元数据上下文
    plan: AgentPlan          # 生成的SQL执行计划
    validation: PlanValidationResult  # 策略校验结果
    reviewer_decision: ReviewerDecision  # AI审核结果
    execution: AgentExecutionResult  # SQL执行结果
    answer: str              # 最终答案
    revision_count: int      # 计划修订次数
    events: list[dict]       # 审计日志（记录每一步发生了什么）
    status: str              # 当前状态（started, failed等）
    error: str               # 错误信息
    execution_time: float    # 总执行时间
    stage_timings: dict      # 各阶段耗时


class ControlledAgentGraph:
    """
    一个有界图，用于准备和批准计划，但从不执行 SQL。
    """

    def __init__(
        self,
        *,
        preparation_service: AgentPreparationService | None = None,
        planner: Planner | None = None,
        reviewer: Reviewer | None = None,
        executor: ControlledSingleSourceExecutor | None = None,
        summarizer: Summarizer | None = None,
        audit_writer: Callable[..., None] = log_agent_trace,
    ) -> None:
        self.preparation_service = preparation_service or AgentPreparationService()
        self.planner = planner or QwenPlanner()
        self.reviewer = reviewer or QwenPlanReviewer()
        self.executor = executor or ControlledSingleSourceExecutor()
        self.summarizer = summarizer or self.executor.sql_generator
        self.audit_writer = audit_writer
        self.graph = self._build_graph()

    # 2. 图结构（_build_graph）
    def _build_graph(self):
        # START → retrieve → plan → validate → review → execute → summarize → END
        # 注意：这不是一条直线！有条件边允许循环和提前结束。

        graph = StateGraph(AgentGraphState)
        # 查找相关数据源和表结构
        graph.add_node("retrieve", self._retrieve)
        # 用AI生成SQL执行计划
        graph.add_node("plan", self._plan)
        # 检查计划是否符合安全策略
        graph.add_node("validate", self._validate)
        # AI审核员检查计划质量
        graph.add_node("review", self._review)
        # 执行SQL查询
        graph.add_node("execute", self._execute)
        # 将查询结果转为自然语言答案
        graph.add_node("summarize", self._summarize)

        # 3. 条件边（决策逻辑）
        graph.add_edge(START, "retrieve")
        # _after_retrieve：如果找不到上下文 → 直接END（需要用户澄清）
        graph.add_conditional_edges("retrieve", self._after_retrieve, {"plan": "plan", "end": END})
        # _after_plan：如果规划失败 → END，否则 → validate
        graph.add_conditional_edges("plan", self._after_plan, {"validate": "validate", "end": END})
        # _after_validation：
        # approved → review
        # revise（需修改）且未超最大重试次数 → 回到 plan
        # 其他情况 → END
        graph.add_conditional_edges("validate", self._after_validation, {"review": "review", "plan": "plan", "end": END})
        # _after_review：
        # revise（需修改）且未超最大重试次数 → 回到 plan
        # approve 且不是仅记录模式 → execute
        # 其他情况 → END
        graph.add_conditional_edges("review", self._after_review, {"plan": "plan", "execute": "execute", "end": END})
        # _after_execution：执行成功 → summarize，失败 → END
        graph.add_conditional_edges("execute", self._after_execution, {"summarize": "summarize", "end": END})
        graph.add_edge("summarize", END)
        return graph.compile()

    async def run(self, question: str, source_hint: str | None = None) -> AgentGraphState:
        # 记录开始时间
        started = time.perf_counter()
        # 初始化状态
        # 调用 self.graph.ainvoke() 开始执行整个工作流
        state = await self.graph.ainvoke({
            "request_id": str(uuid.uuid4()),
            "question": question,
            "source_hint": source_hint or "",
            "revision_count": 0,
            "events": [],
            "status": "started",
        })
        # 计算总执行时间
        execution_time = round(time.perf_counter() - started, 3)
        stage_timings = _stage_timings(state.get("events", []), execution_time)
        state["execution_time"] = execution_time
        state["stage_timings"] = stage_timings
        plan = state.get("plan")
        self.audit_writer(
            request_id=state["request_id"],
            question=state["question"],
            status=state.get("status", "unknown"),
            route_mode=plan.route_mode if plan else None,
            final_plan=plan.model_dump() if plan else None,
            error_message=state.get("error"),
            execution=state.get("execution").model_dump() if state.get("execution") else None,
            execution_time=execution_time,
            stage_timings=stage_timings,
            model_id=settings.agent_sql_model_id,
            events=state.get("events", []),
            candidates=[candidate.model_dump() for candidate in state.get("candidates", [])],
            contexts=[
                {
                    "source": context.source.model_dump(),
                    "schema_closure_object_ids": context.schema_closure_object_ids,
                }
                for context in state.get("contexts", [])
            ],
        )
        execution = state.get("execution")
        write_agent_execution_trace(
            request_id=state["request_id"],
            question=state["question"],
            status=state.get("status", "unknown"),
            candidates=[candidate.model_dump() for candidate in state.get("candidates", [])],
            contexts=[
                {
                    "source": context.source.model_dump(),
                    "schema_signature": context.schema_signature,
                    "selected_object_ids": context.selected_object_ids,
                    "schema_closure_object_ids": context.schema_closure_object_ids,
                    "lexical_selected_object_ids": context.lexical_selected_object_ids,
                    "semantic_object_hits": context.semantic_object_hits,
                    "tables": context.tables,
                }
                for context in state.get("contexts", [])
            ],
            plan=plan.model_dump() if plan else None,
            validation=state["validation"].model_dump() if state.get("validation") else None,
            review=state["reviewer_decision"].model_dump() if state.get("reviewer_decision") else None,
            execution=execution.model_dump(exclude={"results"}) if execution else None,
            answer=state.get("answer"),
            events=state.get("events", []),
            error=state.get("error"),
            stage_timings=stage_timings,
        )
        # 返回最终状态
        return state

    async def _retrieve(self, state: AgentGraphState) -> dict[str, Any]:
        """

        :param state : AgentGraphState：输入参数，当前工作流的状态（包含用户问题、历史记录等）
        :return: 返回值，一个字典，用来更新状态中的部分字段
        """
        started = time.perf_counter()
        lexical_input = {
            "tokens": sorted(tokenize_question(state["question"])),
            "chinese_bigrams": sorted(extract_chinese_bigrams(state["question"].lower())),
            "source_hint": state.get("source_hint") or None,
        }
        try:
            # candidates：候选数据源列表（哪些数据库/表可能包含答案）
            # contexts：上下文列表（具体的表结构、字段信息等）
            candidates, contexts = await asyncio.to_thread(
                self.preparation_service.prepare,
                # 从当前状态中获取用户的原始问题
                state["question"],
                # 如果用户指定了数据源（如 data_source="mysql_prod"），就只在那个数据源中查找；
                # 如果没指定，就搜索所有数据源
                state.get("source_hint") or None,
            )
        except Exception as exc:
            return self._failed(state, "retrieval_failed", exc)

        # 构建审计
        retrieval_event = {
            "node": "retrieve",
            "status": "ok" if contexts else "no_context",
            "candidate_count": len(candidates),
            "source_count": len(contexts),
            "candidates": [
                {
                    "source_id": candidate.source_id,
                    "retrieval_method": candidate.retrieval_method,
                    "matched_terms": candidate.matched_terms,
                    "lexical_score": candidate.lexical_score,
                    "semantic_score": candidate.semantic_score,
                    "hybrid_score": candidate.hybrid_score,
                }
                for candidate in candidates
            ],
            "schema_selection": [
                {
                    "source_id": context.source.source_id,
                    "lexical_selected_object_ids": context.lexical_selected_object_ids,
                    "semantic_object_hits": context.semantic_object_hits,
                    "selected_object_ids": context.selected_object_ids,
                    "schema_closure_object_ids": context.schema_closure_object_ids,
                }
                for context in contexts
            ],
            "retrieval_input": lexical_input,
            "duration_ms": _elapsed_ms(started),
        }

        # "candidates": candidates：保留候选列表（可能有候选但没找到具体表结构）
        # "contexts": []：上下文设为空列表
        # "status": "needs_clarification"：关键！ 状态标记为"需要澄清"
        # 这意味着用户的问题可能太模糊，或者没有对应的数据表
        # 后续的条件边 _after_retrieve 会检测到这个状态，直接结束流程（END）
        # "events": state["events"] + [retrieval_event]：将新事件追加到历史事件列表
        if not contexts:
            return {"candidates": candidates, "contexts": [], "status": "needs_clarification", "events": state["events"] + [retrieval_event]}
        return {"candidates": candidates, "contexts": contexts, "events": state["events"] + [retrieval_event]}

    async def _plan(self, state: AgentGraphState) -> dict[str, Any]:

        # 如果上游节点（retrieve）已经标记状态为 failed 或 needs_clarification，直接返回空字典
        if state.get("status") in {"failed", "needs_clarification"}:
            return {}

        # 从 state["validation"] 中提取策略校验的拒绝原因（如 UNAUTHORIZED_SOURCE、UNSUPPORTED_FUSION_KEY）
        # 审核员修订原因：如果上游 review 节点返回了 revise 决定，将审核员的原因码也加入列表
        # 这些原因会传递给规划器，让它知道之前为什么被拒绝，从而生成修正后的计划
        reasons = list(state.get("validation", PlanValidationResult(status="approved")).reason_codes)
        prior_review = state.get("reviewer_decision")
        if prior_review and prior_review.decision == "revise":
            reasons.extend(prior_review.reason_codes)

        # 如果 state 中已经存在一个 plan（说明这是循环回来的修订），修订次数 +1
        # 首次生成计划时，revision_count 保持为 0
        # 这个计数器用于防止无限循环（受 settings.agent_max_plan_revisions 限制，默认 1 次）
        revision_count = state.get("revision_count", 0) + (1 if state.get("plan") else 0)
        started = time.perf_counter()
        try:
            # 调用 QwenPlanner.plan() 方法，传入：
            # question: 用户原始问题
            # candidates: 候选数据源列表（来自 _retrieve）
            # contexts: 元数据上下文列表（来自 _retrieve）
            # reasons: 修订原因列表（可能为空）
            # 底层会调用 DashScope 的 Qwen 模型（默认 qwen3.7-max），要求输出符合 AgentPlan 契约的 JSON。
            plan = await self.planner.plan(state["question"], state["candidates"], state["contexts"], reasons)
        except AgentLLMError as exc:
            return self._failed(state, "planner_failed", exc)
        if reasons and not plan.revision_summary_zh:
            plan.revision_summary_zh = _reason_summary_zh(reasons)
        return {
            "plan": plan,
            "revision_count": revision_count,
            "events": state["events"] + [{
                "node": "plan", "status": "ok", "revision_count": revision_count,
                "revision_reasons": reasons, "revision_summary_zh": plan.revision_summary_zh,
                "planner_reply": plan.model_dump(), "plan": plan.model_dump(), "duration_ms": _elapsed_ms(started),
            }],
        }

    async def _validate(self, state: AgentGraphState) -> dict[str, Any]:
        if state.get("status") == "failed":
            return {}
        started = time.perf_counter()
        validation = validate_plan(state["plan"], state["contexts"])
        if validation.reason_codes and not validation.reason_summary_zh:
            validation.reason_summary_zh = _reason_summary_zh(validation.reason_codes)
        return {
            "validation": validation,
            "status": "validated" if validation.status == "approved" else validation.status,
            "events": state["events"] + [{
                "node": "validate", "status": validation.status, "reason_codes": validation.reason_codes,
                "reason_summary_zh": validation.reason_summary_zh,
                "plan": state["plan"].model_dump(), "duration_ms": _elapsed_ms(started),
            }],
        }

    async def _review(self, state: AgentGraphState) -> dict[str, Any]:
        # 条件：如果上游 validate 节点的结果不是 approved（即 revise 或 rejected）
        # 动作：直接返回空字典 {}
        # 效果：不覆盖 state 中的任何字段，保留上游的 status 和 validation
        # 原因：策略校验已经拒绝或要求修订的计划，不需要再经过 AI 审核
        if state.get("validation", PlanValidationResult(status="rejected")).status != "approved":
            return {}
        started = time.perf_counter()
        try:
            # Qwen 审核角色会检查：
            # 计划是否忠实于用户问题
            # 是否只使用了给定的 metadata
            # 是否存在不可靠的跨源融合
            # 是否存在越权操作
            decision = await self.reviewer.review(state["question"], state["plan"], state["contexts"])
        except AgentLLMError as exc:
            return self._failed(state, "reviewer_failed", exc)

        if decision.reason_codes and not decision.reason_summary_zh:
            decision.reason_summary_zh = _reason_summary_zh(decision.reason_codes)

        # approve	approved_record_only	审核通过（仅记录模式）
        # revise	review_revision_requested	审核员要求修订
        # reject	review_rejected	审核员拒绝
        status = {
            "approve": "approved_record_only",
            "revise": "review_revision_requested",
            "reject": "review_rejected",
        }[decision.decision]
        # reviewer_decision: 审核决策对象（包含 decision 和 reason_codes）
        # status: 映射后的工作流状态
        # events: 追加审计日志，记录审核结果和原因码
        return {
            "reviewer_decision": decision,
            "status": status,
            "events": state["events"] + [{
                "node": "review", "status": decision.decision, "reason_codes": decision.reason_codes,
                "reason_summary_zh": decision.reason_summary_zh,
                "reviewer_reply": decision.model_dump(), "decision": decision.model_dump(), "duration_ms": _elapsed_ms(started),
            }],
        }

    async def _execute(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            execution = await self.executor.execute(state["question"], state["plan"], state["contexts"])
        except Exception as exc:
            return self._failed(state, "execution_node_failed", exc)
        status = "executed_success" if execution.success else "executed_failed"
        return {
            "execution": execution,
            "status": status,
            "events": state["events"] + [{
                "node": "execute", "status": status, "row_count": execution.row_count,
                "retry_attempted": execution.retry_attempted, "error": execution.error,
                "generation": execution.generation_trace, "duration_ms": _elapsed_ms(started),
            }],
        }

    async def _summarize(self, state: AgentGraphState) -> dict[str, Any]:
        execution = state["execution"]
        started = time.perf_counter()
        try:
            answer = await self.summarizer.summarize_result(
                state["question"], execution.columns, execution.results,
                # 使用的AI模型ID
                answer_template="brief", custom_instruction="", model_id=None, model_config=None,
            )
        except Exception as exc:
            return self._failed(state, "summary_failed", exc)
        return {
            "answer": answer,
            "status": "executed_summarized",
            "events": state["events"] + [{"node": "summarize", "status": "ok", "duration_ms": _elapsed_ms(started)}],
        }

    def _after_validation(self, state: AgentGraphState) -> str:
        if state.get("status") in {"failed", "needs_clarification", "rejected"}:
            return "end"
        validation = state["validation"]
        if validation.status == "approved":
            return "review"
        # 计划最大修订次数（防止无限循环）
        if validation.status == "revise" and state.get("revision_count", 0) < settings.agent_max_plan_revisions:
            return "plan"
        return "end"

    @staticmethod
    def _after_retrieve(state: AgentGraphState) -> str:
        return "end" if state.get("status") in {"failed", "needs_clarification"} else "plan"

    @staticmethod
    def _after_plan(state: AgentGraphState) -> str:
        return "end" if state.get("status") == "failed" else "validate"

    def _after_review(self, state: AgentGraphState) -> str:
        if state.get("status") == "failed":
            return "end"
        decision = state["reviewer_decision"]
        if decision.decision == "revise" and state.get("revision_count", 0) < settings.agent_max_plan_revisions:
            return "plan"
        # 如果为 True，审核通过后不执行SQL，直接结束（仅记录模式）
        if decision.decision == "approve" and not settings.agent_record_only:
            return "execute"
        return "end"

    @staticmethod
    def _after_execution(state: AgentGraphState) -> str:
        execution = state.get("execution")
        return "summarize" if execution and execution.success else "end"

    @staticmethod
    def _failed(state: AgentGraphState, status: str, exc: Exception) -> dict[str, Any]:
        # 错误信息使用安全的操作码（如 planner_failed、reviewer_failed），
        # 不暴露AI提供商的原始响应内容或密钥，前端可以根据这些码区分不同的失败类型。
        detail = str(exc).strip() or type(exc).__name__
        return {
            "status": "failed",
            "error": f"{status}:{detail}",
            "events": state["events"] + [{"node": status, "status": "failed", "error": detail}],
        }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _stage_timings(events: list[dict[str, Any]], total_seconds: float) -> dict[str, float]:
    """Aggregate every node attempt, including plan/review revisions, for audit."""
    timings: dict[str, float] = {"total": total_seconds}
    for event in events:
        duration_ms = event.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            node = str(event.get("node", "unknown"))
            timings[node] = round(timings.get(node, 0.0) + float(duration_ms) / 1000, 3)
    return timings
