import asyncio

from backend.agent.contracts import AgentPlan, AgentSubtask, MetadataContext, SourceDescriptor, SqlRepairProposal, SqlReviewDecision
from backend.agent.execution import (
    ControlledSingleSourceExecutor,
    repair_sql_month_scope,
    validate_sql_latest_price_scope,
    validate_sql_month_scope,
    validate_sql_scope,
)


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


def test_month_scope_repair_corrects_one_unambiguous_upper_bound():
    question = "查询 2026 年 1 月报警中涉及嫌疑人张三的报警记录。"
    wrong_sql = (
        "SELECT a.alert_no FROM police_alert a "
        "WHERE a.alert_time >= '2026-01-01 00:00:00' "
        "AND a.alert_time < '2027-01-01 00:00:00'"
    )

    repaired_sql = repair_sql_month_scope(question, wrong_sql, dialect="mysql")

    assert repaired_sql is not None
    assert "2026-02-01 00:00:00" in repaired_sql
    assert "2027-01-01" not in repaired_sql
    assert validate_sql_month_scope(question, repaired_sql) == (True, None)


def test_month_scope_repair_refuses_ambiguous_date_columns():
    question = "查询 2026 年 1 月的报警记录。"
    ambiguous_sql = (
        "SELECT a.alert_no FROM police_alert a "
        "WHERE a.alert_time >= '2026-01-01' AND a.alert_time < '2027-01-01' "
        "AND a.incident_time >= '2026-01-01' AND a.incident_time < '2027-01-01'"
    )

    assert repair_sql_month_scope(question, ambiguous_sql, dialect="mysql") is None


def test_latest_stock_price_cannot_be_filtered_to_a_historical_date():
    question = "苹果公司最新收盘价和涨跌幅。"
    stale_sql = (
        "SELECT close_price FROM v_stock_latest_price "
        "WHERE trade_date >= '2023-01-01' AND trade_date < '2024-01-01'"
    )
    current_sql = "SELECT close_price, change_pct FROM v_stock_latest_price WHERE chinese_name = '苹果公司'"

    assert validate_sql_latest_price_scope(question, stale_sql, source_id="postgres_stock") == (
        False,
        "LATEST_PRICE_MUST_NOT_FILTER_HISTORICAL_DATE",
    )
    assert validate_sql_latest_price_scope(question, current_sql, source_id="postgres_stock") == (True, None)


def test_latest_stock_price_allows_a_separate_historical_average_subquery():
    question = "找出最新收盘价高于自身 2026 年 7 月平均收盘价的科技股。"
    sql = (
        "SELECT l.close_price FROM v_stock_latest_price l "
        "WHERE l.close_price > (SELECT AVG(h.close_price) FROM v_stock_price_detail h "
        "WHERE h.trade_date >= DATE '2026-07-01' AND h.trade_date < DATE '2026-08-01')"
    )

    assert validate_sql_latest_price_scope(question, sql, source_id="postgres_stock") == (True, None)


def test_executor_runs_only_guarded_sql_from_approved_single_source_plan():
    class FakeGenerator:
        async def generate_controlled_sql(self, *args, **kwargs):
            prompt_context = args[0]
            assert prompt_context.task_goal == "查询员工"
            assert prompt_context.required_object_ids == ["employees"]
            return "SELECT name FROM employees", "", None, {"prompt_template": "controlled prompt"}

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
    assert result.generation_trace["prompt_template"]


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


def test_executor_deterministically_repairs_calendar_month_before_model_repair():
    class FakeGenerator:
        async def generate_controlled_sql(self, *args, **kwargs):
            return (
                "SELECT a.alert_no FROM police_alert a "
                "WHERE a.alert_time >= '2026-01-01 00:00:00' "
                "AND a.alert_time < '2027-01-01 00:00:00'",
                "",
                None,
                {},
            )

    class UnexpectedDiagnostician:
        async def diagnose(self, **kwargs):
            raise AssertionError("deterministic month repair must not call the model diagnostician")

    class UnexpectedReviewer:
        async def review(self, **kwargs):
            raise AssertionError("deterministic month repair must not call the model reviewer")

    class FakeAdapter:
        def execute_query(self, sql):
            assert "2026-02-01 00:00:00" in sql
            assert "2027-01-01" not in sql
            return [{"alert_no": "AL001"}], ["alert_no"]

    context = MetadataContext(
        source=SourceDescriptor(
            source_id="mysql_police_address",
            source_type="mysql",
            dialect="mysql",
            description="test",
            capabilities=["readonly_sql"],
        ),
        selected_object_ids=["police_alert"],
        schema_closure_object_ids=["police_alert"],
        tables=[{
            "name": "police_alert",
            "columns": [
                {"name": "alert_no"},
                {"name": "alert_time"},
            ],
        }],
    )
    plan = AgentPlan(
        route_mode="single_source",
        subtasks=[AgentSubtask(
            id="query_alerts",
            source_id="mysql_police_address",
            operation_id="readonly_sql",
            goal="查询 2026 年 1 月报警记录",
            object_ids=["police_alert"],
            output_fields=["police_alert.alert_no"],
        )],
        confidence=0.9,
    )

    result = asyncio.run(
        ControlledSingleSourceExecutor(
            sql_generator=FakeGenerator(),
            diagnostician=UnexpectedDiagnostician(),
            reviewer=UnexpectedReviewer(),
            adapter_provider=lambda _: FakeAdapter(),
        ).execute("查询 2026 年 1 月报警记录", plan, [context])
    )

    assert result.success is True
    assert result.retry_attempted is True
    assert result.repair_trace["strategy"] == "deterministic_calendar_month"
    assert result.repair_trace["status"] == "applied"
