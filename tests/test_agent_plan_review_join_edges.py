import asyncio

from backend.agent.contracts import AgentPlan, MetadataContext, SourceDescriptor
from backend.agent.llm import QwenPlanReviewer
import backend.agent.llm as agent_llm


def _plan() -> AgentPlan:
    return AgentPlan.model_validate({
        "route_mode": "single_source",
        "confidence": 0.95,
        "subtasks": [{
            "id": "find_closed_alerts_no_event",
            "source_id": "mysql_police_address",
            "operation_id": "readonly_sql",
            "goal": "找出已结案但尚未关联案件的报警",
            "object_ids": ["police_alert", "alert_event"],
            "output_fields": ["police_alert.alert_no"],
        }],
    })


def _context(with_join: bool = True) -> MetadataContext:
    event_foreign_keys = [{
        "column": "alert_no",
        "ref_table": "police_alert",
        "ref_column": "alert_no",
    }] if with_join else []
    return MetadataContext(
        source=SourceDescriptor(
            source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警务数据",
        ),
        selected_object_ids=["police_alert", "alert_event"],
        schema_closure_object_ids=["police_alert", "alert_event"],
        tables=[
            {"name": "police_alert", "columns": [{"name": "alert_no"}], "foreign_keys": []},
            {"name": "alert_event", "columns": [{"name": "alert_no"}], "foreign_keys": event_foreign_keys},
        ],
    )


def test_verified_join_edge_is_passed_to_qwen_and_overrides_missing_join_key(monkeypatch):
    captured = {}

    async def fake_request_json(**kwargs):
        captured.update(kwargs["user_payload"])
        return {"decision": "revise", "reason_codes": ["MISSING_JOIN_KEY"]}

    monkeypatch.setattr(agent_llm, "_request_json", fake_request_json)
    decision = asyncio.run(QwenPlanReviewer().review("核查报警", _plan(), [_context()]))

    assert captured["join_edges"] == [{
        "source_id": "mysql_police_address",
        "from_table": "alert_event",
        "from_column": "alert_no",
        "to_table": "police_alert",
        "to_column": "alert_no",
    }]
    assert decision.decision == "approve"
    assert decision.reason_codes == []


def test_missing_join_key_is_not_overridden_without_a_verified_edge(monkeypatch):
    async def fake_request_json(**_kwargs):
        return {"decision": "revise", "reason_codes": ["MISSING_JOIN_KEY"]}

    monkeypatch.setattr(agent_llm, "_request_json", fake_request_json)
    decision = asyncio.run(QwenPlanReviewer().review("核查报警", _plan(), [_context(with_join=False)]))

    assert decision.decision == "revise"
    assert decision.reason_codes == ["MISSING_JOIN_KEY"]
