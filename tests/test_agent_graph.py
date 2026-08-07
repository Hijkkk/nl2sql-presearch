import asyncio

from backend.agent.contracts import AgentPlan, MetadataContext, ReviewerDecision, SourceCandidate, SourceDescriptor
from backend.agent.graph import ControlledAgentGraph
from backend.agent.contracts import AgentExecutionResult
from backend.config.config import settings


def _prepared_data():
    candidate = SourceCandidate(
        source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警情数据", score=5
    )
    context = MetadataContext(
        source=SourceDescriptor(source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警情数据"),
        selected_object_ids=["alerts"],
        schema_closure_object_ids=["alerts"],
        tables=[{"name": "alerts", "columns": [{"name": "city"}]}],
    )
    return [candidate], [context]


class FakePreparation:
    def prepare(self, question):
        return _prepared_data()


class ApprovingPlanner:
    def __init__(self):
        self.calls = 0

    async def plan(self, question, candidates, contexts, revision_reasons=None):
        self.calls += 1
        return AgentPlan.model_validate({
            "route_mode": "single_source", "confidence": 0.9,
            "subtasks": [{"id": "police_counts", "source_id": "mysql_police_address", "operation_id": "readonly_sql", "goal": "统计警情", "object_ids": ["alerts"], "output_fields": ["alerts.city"]}],
        })


class ApprovingReviewer:
    async def review(self, question, plan, contexts):
        return ReviewerDecision(decision="approve")


class ReviseOnceReviewer:
    def __init__(self):
        self.calls = 0

    async def review(self, question, plan, contexts):
        self.calls += 1
        return ReviewerDecision(decision="revise", reason_codes=["QUESTION_SCOPE_CHANGED"]) if self.calls == 1 else ReviewerDecision(decision="approve")


class FailingPreparation:
    def prepare(self, question):
        raise RuntimeError("metadata unavailable")


class FakeExecutor:
    async def execute(self, question, plan, contexts):
        return AgentExecutionResult(success=True, sql="SELECT 1", columns=["value"], results=[{"value": 1}], row_count=1)


class FakeSummarizer:
    async def summarize_result(self, question, columns, results, **kwargs):
        assert columns == ["value"]
        assert results == [{"value": 1}]
        return "查询成功，共 1 条记录。"


def test_langgraph_approves_a_valid_plan_without_executing_sql(monkeypatch):
    monkeypatch.setattr(settings, "agent_record_only", True)
    audit_calls = []
    graph = ControlledAgentGraph(
        preparation_service=FakePreparation(), planner=ApprovingPlanner(), reviewer=ApprovingReviewer(),
        audit_writer=lambda **payload: audit_calls.append(payload),
    )

    state = asyncio.run(graph.run("统计本月警情"))

    assert state["status"] == "approved_record_only"
    assert [event["node"] for event in state["events"]] == ["retrieve", "plan", "validate", "review"]
    assert audit_calls[0]["request_id"] == state["request_id"]
    assert audit_calls[0]["route_mode"] == "single_source"


def test_langgraph_allows_exactly_one_reviewer_requested_revision(monkeypatch):
    monkeypatch.setattr(settings, "agent_record_only", True)
    planner = ApprovingPlanner()
    reviewer = ReviseOnceReviewer()
    graph = ControlledAgentGraph(preparation_service=FakePreparation(), planner=planner, reviewer=reviewer, audit_writer=lambda **_: None)

    state = asyncio.run(graph.run("统计本月警情"))

    assert state["status"] == "approved_record_only"
    assert planner.calls == 2
    assert reviewer.calls == 2
    assert state["revision_count"] == 1


def test_langgraph_passes_reviewer_reason_to_revision_planner(monkeypatch):
    monkeypatch.setattr(settings, "agent_record_only", True)

    class ReasonCapturingPlanner(ApprovingPlanner):
        def __init__(self):
            super().__init__()
            self.reasons = []

        async def plan(self, question, candidates, contexts, revision_reasons=None):
            self.reasons.append(revision_reasons or [])
            return await super().plan(question, candidates, contexts, revision_reasons)

    planner = ReasonCapturingPlanner()
    graph = ControlledAgentGraph(
        preparation_service=FakePreparation(), planner=planner, reviewer=ReviseOnceReviewer(), audit_writer=lambda **_: None,
    )

    state = asyncio.run(graph.run("统计本月警情"))

    assert state["status"] == "approved_record_only"
    assert planner.reasons == [[], ["QUESTION_SCOPE_CHANGED"]]


def test_langgraph_ends_safely_when_retrieval_fails():
    graph = ControlledAgentGraph(
        preparation_service=FailingPreparation(), planner=ApprovingPlanner(), reviewer=ApprovingReviewer(),
        audit_writer=lambda **_: None,
    )

    state = asyncio.run(graph.run("统计本月警情"))

    assert state["status"] == "failed"
    assert [event["node"] for event in state["events"]] == ["retrieval_failed"]


def test_langgraph_executes_only_when_record_only_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "agent_record_only", False)
    graph = ControlledAgentGraph(
        preparation_service=FakePreparation(), planner=ApprovingPlanner(), reviewer=ApprovingReviewer(),
        executor=FakeExecutor(), summarizer=FakeSummarizer(), audit_writer=lambda **_: None,
    )

    state = asyncio.run(graph.run("统计本月警情"))

    assert state["status"] == "executed_summarized"
    assert state["execution"].sql == "SELECT 1"
    assert state["answer"] == "查询成功，共 1 条记录。"
