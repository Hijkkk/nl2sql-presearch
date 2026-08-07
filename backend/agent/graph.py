"""LangGraph orchestration for the record-only controlled-agent rollout."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agent.contracts import AgentExecutionResult, AgentPlan, MetadataContext, PlanValidationResult, ReviewerDecision, SourceCandidate
from backend.agent.execution import ControlledSingleSourceExecutor
from backend.agent.llm import AgentLLMError, DeepSeekReviewer, QwenPlanner
from backend.agent.policy import validate_plan
from backend.agent.service import AgentPreparationService
from backend.config.audit import log_agent_trace, write_agent_execution_trace
from backend.config.config import settings


class Planner(Protocol):
    async def plan(self, question: str, candidates: list[SourceCandidate], contexts: list[MetadataContext], revision_reasons: list[str] | None = None) -> AgentPlan: ...


class Reviewer(Protocol):
    async def review(self, question: str, plan: AgentPlan, contexts: list[MetadataContext]) -> ReviewerDecision: ...


class Summarizer(Protocol):
    async def summarize_result(self, question: str, columns: list[str], results: list[dict], **kwargs: Any) -> str: ...


class AgentGraphState(TypedDict, total=False):
    request_id: str
    question: str
    candidates: list[SourceCandidate]
    contexts: list[MetadataContext]
    plan: AgentPlan
    validation: PlanValidationResult
    reviewer_decision: ReviewerDecision
    execution: AgentExecutionResult
    answer: str
    revision_count: int
    events: list[dict[str, Any]]
    status: str
    error: str


class ControlledAgentGraph:
    """A bounded graph that prepares and approves plans but never executes SQL."""

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
        self.reviewer = reviewer or DeepSeekReviewer()
        self.executor = executor or ControlledSingleSourceExecutor()
        self.summarizer = summarizer or self.executor.sql_generator
        self.audit_writer = audit_writer
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentGraphState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("plan", self._plan)
        graph.add_node("validate", self._validate)
        graph.add_node("review", self._review)
        graph.add_node("execute", self._execute)
        graph.add_node("summarize", self._summarize)
        graph.add_edge(START, "retrieve")
        graph.add_conditional_edges("retrieve", self._after_retrieve, {"plan": "plan", "end": END})
        graph.add_conditional_edges("plan", self._after_plan, {"validate": "validate", "end": END})
        graph.add_conditional_edges("validate", self._after_validation, {"review": "review", "plan": "plan", "end": END})
        graph.add_conditional_edges("review", self._after_review, {"plan": "plan", "execute": "execute", "end": END})
        graph.add_conditional_edges("execute", self._after_execution, {"summarize": "summarize", "end": END})
        graph.add_edge("summarize", END)
        return graph.compile()

    async def run(self, question: str) -> AgentGraphState:
        state = await self.graph.ainvoke({
            "request_id": str(uuid.uuid4()),
            "question": question,
            "revision_count": 0,
            "events": [],
            "status": "started",
        })
        plan = state.get("plan")
        self.audit_writer(
            request_id=state["request_id"],
            question=state["question"],
            status=state.get("status", "unknown"),
            route_mode=plan.route_mode if plan else None,
            final_plan=plan.model_dump() if plan else None,
            error_message=state.get("error"),
            events=state.get("events", []),
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
        )
        return state

    async def _retrieve(self, state: AgentGraphState) -> dict[str, Any]:
        try:
            candidates, contexts = await asyncio.to_thread(self.preparation_service.prepare, state["question"])
        except Exception as exc:
            return self._failed(state, "retrieval_failed", exc)
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
        }
        if not contexts:
            return {"candidates": candidates, "contexts": [], "status": "needs_clarification", "events": state["events"] + [retrieval_event]}
        return {"candidates": candidates, "contexts": contexts, "events": state["events"] + [retrieval_event]}

    async def _plan(self, state: AgentGraphState) -> dict[str, Any]:
        if state.get("status") in {"failed", "needs_clarification"}:
            return {}
        reasons = list(state.get("validation", PlanValidationResult(status="approved")).reason_codes)
        prior_review = state.get("reviewer_decision")
        if prior_review and prior_review.decision == "revise":
            reasons.extend(prior_review.reason_codes)
        revision_count = state.get("revision_count", 0) + (1 if state.get("plan") else 0)
        try:
            plan = await self.planner.plan(state["question"], state["candidates"], state["contexts"], reasons)
        except AgentLLMError as exc:
            return self._failed(state, "planner_failed", exc)
        return {
            "plan": plan,
            "revision_count": revision_count,
            "events": state["events"] + [{"node": "plan", "status": "ok", "revision_count": revision_count}],
        }

    async def _validate(self, state: AgentGraphState) -> dict[str, Any]:
        if state.get("status") == "failed":
            return {}
        validation = validate_plan(state["plan"], state["contexts"])
        return {
            "validation": validation,
            "status": "validated" if validation.status == "approved" else validation.status,
            "events": state["events"] + [{"node": "validate", "status": validation.status, "reason_codes": validation.reason_codes}],
        }

    async def _review(self, state: AgentGraphState) -> dict[str, Any]:
        if state.get("validation", PlanValidationResult(status="rejected")).status != "approved":
            return {}
        try:
            decision = await self.reviewer.review(state["question"], state["plan"], state["contexts"])
        except AgentLLMError as exc:
            return self._failed(state, "reviewer_failed", exc)
        status = {
            "approve": "approved_record_only",
            "revise": "review_revision_requested",
            "reject": "review_rejected",
        }[decision.decision]
        return {
            "reviewer_decision": decision,
            "status": status,
            "events": state["events"] + [{"node": "review", "status": decision.decision, "reason_codes": decision.reason_codes}],
        }

    async def _execute(self, state: AgentGraphState) -> dict[str, Any]:
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
            }],
        }

    async def _summarize(self, state: AgentGraphState) -> dict[str, Any]:
        execution = state["execution"]
        try:
            answer = await self.summarizer.summarize_result(
                state["question"], execution.columns, execution.results,
                answer_template="brief", custom_instruction="", model_id=None, model_config=None,
            )
        except Exception as exc:
            return self._failed(state, "summary_failed", exc)
        return {
            "answer": answer,
            "status": "executed_summarized",
            "events": state["events"] + [{"node": "summarize", "status": "ok"}],
        }

    def _after_validation(self, state: AgentGraphState) -> str:
        if state.get("status") in {"failed", "needs_clarification", "rejected"}:
            return "end"
        validation = state["validation"]
        if validation.status == "approved":
            return "review"
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
        if decision.decision == "approve" and not settings.agent_record_only:
            return "execute"
        return "end"

    @staticmethod
    def _after_execution(state: AgentGraphState) -> str:
        execution = state.get("execution")
        return "summarize" if execution and execution.success else "end"

    @staticmethod
    def _failed(state: AgentGraphState, status: str, exc: Exception) -> dict[str, Any]:
        # AgentLLMError messages are deliberately safe operational codes.  Keep
        # them in the response so the UI can distinguish a reviewer timeout from
        # a policy rejection without exposing provider response bodies or keys.
        detail = str(exc).strip() or type(exc).__name__
        return {
            "status": "failed",
            "error": f"{status}:{detail}",
            "events": state["events"] + [{"node": status, "status": "failed", "error": detail}],
        }
