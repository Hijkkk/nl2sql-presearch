import asyncio

from backend.agent.contracts import AgentPlan, MetadataContext, SourceDescriptor, SqlRepairProposal, SqlReviewDecision
from backend.agent.execution import ControlledSingleSourceExecutor, validate_sql_month_scope, validate_sql_scope


def _context():
    return MetadataContext(
        source=SourceDescriptor(source_id="sqlite_demo", source_type="sqlite", dialect="sqlite", description="演示"),
        selected_object_ids=["employees"],
        schema_closure_object_ids=["employees"],
        tables=[{"name": "employees", "columns": [{"name": "id"}, {"name": "name"}]}],
    )


def _plan():
    return AgentPlan.model_validate({
        "route_mode": "single_source", "confidence": 0.9,
        "subtasks": [{"id": "employee_names", "source_id": "sqlite_demo", "operation_id": "readonly_sql", "goal": "查询员工", "object_ids": ["employees"]}],
    })


def test_sql_scope_blocks_tables_and_fields_outside_approved_context():
    context = _context()

    assert validate_sql_scope("SELECT name FROM employees", dialect="sqlite", context=context)[0] is True
    assert validate_sql_scope("SELECT salary FROM employees", dialect="sqlite", context=context)[1] == "FIELD_OUTSIDE_SCHEMA_CLOSURE"
    assert validate_sql_scope("SELECT name FROM departments", dialect="sqlite", context=context)[1] == "TABLE_OUTSIDE_SCHEMA_CLOSURE"


def test_month_scope_requires_the_whole_calendar_month():
    question = "\u67e5\u8be2 2026 \u5e74 1 \u6708\u7684\u8bb0\u5f55"
    wrong_sql = "SELECT * FROM police_alert WHERE alert_time >= '2026-01-01 00:00:00' AND alert_time < '2026-01-02 00:00:00'"
    correct_sql = "SELECT * FROM police_alert WHERE alert_time >= '2026-01-01 00:00:00' AND alert_time < '2026-02-01 00:00:00'"

    assert validate_sql_month_scope(question, wrong_sql) == (False, "MONTH_TIME_RANGE_REQUIRED:2026-01")
    assert validate_sql_month_scope(question, correct_sql) == (True, None)


def test_executor_runs_only_guarded_sql_from_approved_single_source_plan():
    class FakeGenerator:
        async def generate_controlled_sql(self, *args, **kwargs):
            prompt_context = args[0]
            assert prompt_context.task_goal == "查询员工"
            assert prompt_context.required_object_ids == ["employees"]
            return "SELECT name FROM employees", "", None, {}

    class FakeAdapter:
        def execute_query(self, sql):
            assert "LIMIT 1000" in sql.upper()
            return [{"name": "张三"}], ["name"]

    result = asyncio.run(
        ControlledSingleSourceExecutor(sql_generator=FakeGenerator(), adapter_provider=lambda _: FakeAdapter()).execute(
            "查询员工", _plan(), [_context()]
        )
    )

    assert result.success is True
    assert result.row_count == 1
    assert result.retry_attempted is False


def test_executor_retries_once_only_after_diagnosis_and_independent_approval():
    class FakeGenerator:
        async def generate_controlled_sql(self, *args, **kwargs):
            return "SELECT salary FROM employees", "", None, {}

    class FakeDiagnostician:
        async def diagnose(self, **kwargs):
            assert kwargs["error_message"] == "FIELD_OUTSIDE_SCHEMA_CLOSURE"
            return SqlRepairProposal(diagnosis="字段不存在", can_retry=True, proposed_sql="SELECT name FROM employees", risk="low")

    class FakeReviewer:
        async def review(self, **kwargs):
            return SqlReviewDecision(approve=True)

    class FakeAdapter:
        def execute_query(self, sql):
            return [{"name": "张三"}], ["name"]

    result = asyncio.run(
        ControlledSingleSourceExecutor(
            sql_generator=FakeGenerator(), diagnostician=FakeDiagnostician(), reviewer=FakeReviewer(), adapter_provider=lambda _: FakeAdapter()
        ).execute("查询员工", _plan(), [_context()])
    )

    assert result.success is True
    assert result.retry_attempted is True
