from backend.agent.contracts import AgentPlan, MetadataContext, SourceDescriptor
from backend.agent.policy import validate_plan
from backend.agent.llm import _plan_contract_error_summary, _plan_goal_exactly_preserves_question


def _context(source_id="mysql_police_address", source_type="mysql"):
    return MetadataContext(
        source=SourceDescriptor(source_id=source_id, source_type=source_type, dialect="mysql", description="警情数据"),
        selected_object_ids=["alerts"],
        schema_closure_object_ids=["alerts"],
        tables=[{"name": "alerts", "columns": [{"name": "city"}, {"name": "count"}]}],
    )


def test_policy_approves_a_single_source_plan_inside_metadata_context():
    plan = AgentPlan.model_validate({
        "route_mode": "single_source",
        "confidence": 0.9,
        "subtasks": [{
            "id": "police_counts", "source_id": "mysql_police_address", "operation_id": "readonly_sql",
            "goal": "按城市统计警情", "object_ids": ["alerts"], "output_fields": ["alerts.city", "alerts.count"],
        }],
    })

    assert validate_plan(plan, [_context()]).status == "approved"


def test_policy_rejects_an_unapproved_source_and_field():
    plan = AgentPlan.model_validate({
        "route_mode": "single_source", "confidence": 0.9,
        "subtasks": [{
            "id": "bad_task", "source_id": "postgres_stock", "operation_id": "readonly_sql",
            "goal": "查询", "object_ids": ["secret"], "output_fields": ["secret.token"],
        }],
    })

    result = validate_plan(plan, [_context()])

    assert result.status == "rejected"
    assert "UNAUTHORIZED_SOURCE:postgres_stock" in result.reason_codes


def test_policy_requests_revision_for_a_field_outside_schema_closure():
    plan = AgentPlan.model_validate({
        "route_mode": "single_source", "confidence": 0.9,
        "subtasks": [{
            "id": "police_counts", "source_id": "mysql_police_address", "operation_id": "readonly_sql",
            "goal": "按城市统计警情", "object_ids": ["alerts"], "output_fields": ["alerts.secret_field"],
        }],
    })

    result = validate_plan(plan, [_context()])

    assert result.status == "revise"
    assert result.reason_codes == ["FIELD_OUTSIDE_SCHEMA_CLOSURE:police_counts"]


def test_policy_allows_an_aggregate_over_an_approved_field():
    plan = AgentPlan.model_validate({
        "route_mode": "single_source", "confidence": 0.9,
        "subtasks": [{
            "id": "police_counts", "source_id": "mysql_police_address", "operation_id": "readonly_sql",
            "goal": "统计警情", "object_ids": ["alerts"], "output_fields": ["COUNT(alerts.count)"],
        }],
    })

    assert validate_plan(plan, [_context()]).status == "approved"


def test_policy_allows_a_second_alias_for_a_verified_self_join():
    context = MetadataContext(
        source=SourceDescriptor(source_id="sqlite_demo", source_type="sqlite", dialect="sqlite", description="员工数据"),
        selected_object_ids=["employees"],
        schema_closure_object_ids=["employees"],
        tables=[{
            "name": "employees",
            "columns": [{"name": "id"}, {"name": "name"}, {"name": "manager_id"}],
            "foreign_keys": [{"column": "manager_id", "ref_table": "employees", "ref_column": "id"}],
        }],
    )
    plan = AgentPlan.model_validate({
        "route_mode": "single_source", "confidence": 0.95,
        "subtasks": [{
            "id": "list_employees_with_managers", "source_id": "sqlite_demo", "operation_id": "readonly_sql",
            "goal": "列出每位有直属经理的员工及其经理姓名", "object_ids": ["employees"],
            "output_fields": ["employees.name", "managers.name"],
        }],
    })

    assert validate_plan(plan, [context]).status == "approved"


def test_policy_allows_one_revision_when_a_multi_source_merge_contract_is_missing():
    plan = AgentPlan.model_validate({
        "route_mode": "multi_source", "confidence": 0.8,
        "subtasks": [{
            "id": "police_counts", "source_id": "mysql_police_address", "operation_id": "readonly_sql",
            "goal": "按城市统计警情", "object_ids": ["alerts"],
        }],
    })

    result = validate_plan(plan, [_context()])

    assert result.status == "revise"
    assert result.reason_codes == ["MISSING_MERGE_CONTRACT"]


def test_exact_goal_preserves_question_for_plan_stage_scope_review():
    plan = AgentPlan.model_validate({
        "route_mode": "single_source", "confidence": 0.9,
        "subtasks": [{
            "id": "alert_count", "source_id": "mysql_police_address", "operation_id": "readonly_sql",
            "goal": "统计2026年1月东城区已结案的治安报警数量", "object_ids": ["alerts"],
        }],
    })

    assert _plan_goal_exactly_preserves_question("统计 2026 年 1 月东城区已结案的治安报警数量？", plan)


def test_plan_contract_error_summary_exposes_only_field_and_rule():
    try:
        AgentPlan.model_validate({"route_mode": "single-source", "subtasks": []})
    except ValueError as exc:
        summary = _plan_contract_error_summary(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid plan unexpectedly passed validation")

    assert "route_mode:literal_error" in summary
    assert "subtasks:too_short" in summary


def test_policy_rejects_cyclic_dependencies():
    plan = AgentPlan.model_validate({
        "route_mode": "multi_source", "merge_contract_id": "city_key_left_join", "confidence": 0.8,
        "subtasks": [
            {"id": "one", "source_id": "mysql_police_address", "operation_id": "readonly_sql", "goal": "一", "depends_on": ["two"]},
            {"id": "two", "source_id": "mysql_police_address", "operation_id": "readonly_sql", "goal": "二", "depends_on": ["one"]},
        ],
    })

    result = validate_plan(plan, [_context()])

    assert result.status == "rejected"
    assert "DEPENDENCY_CYCLE" in result.reason_codes
