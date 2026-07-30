import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.adapters.postgres_adapter import PostgreSQLAdapter
from backend.config.audit import audit_db_path, prepare_result_sample
from backend.models.models import ChatResponse
from backend.nl2sql.prompt_builder import PromptBuilder
from backend.nl2sql.sql_generator import SQLGenerator
from backend.routers.chat_module import response_for_client
from backend.config.config import settings


def test_sql_extractor_accepts_select_with_and_code_block():
    generator = SQLGenerator()

    _, sql = generator._extract_thought_and_sql("SELECT COUNT(*) FROM employees;")
    assert sql == "SELECT COUNT(*) FROM employees;"

    _, sql = generator._extract_thought_and_sql("WITH x AS (SELECT 1) SELECT * FROM x;")
    assert sql == "WITH x AS (SELECT 1) SELECT * FROM x;"

    thought, sql = generator._extract_thought_and_sql("说明\n```sql\nSELECT 1;\n```")
    assert thought == "说明"
    assert sql == "SELECT 1;"

    thought, sql = generator._extract_thought_and_sql("没有可用 SQL")
    assert thought == "没有可用 SQL"
    assert sql == ""


def test_template_summary_handles_empty_aggregate_and_topn():
    generator = SQLGenerator()

    assert generator._try_template_summary("查一下", ["id"], []) == "没有查询到符合条件的数据。"
    assert generator._try_template_summary("统计数量", ["数量"], [{"数量": 3}]) == "数量为 3。"

    topn = generator._try_template_summary(
        "成交量最高的前 10 只股票是哪些？",
        ["symbol", "volume"],
        [{"symbol": "AAA", "volume": 100}, {"symbol": "BBB", "volume": 90}],
    )
    assert "查询到 2 条记录" in topn
    assert "AAA" in topn

    small_table = generator._try_template_summary(
        "inspect departments",
        ["department_name", "employee_count"],
        [{"department_name": "engineering", "employee_count": 3}],
    )
    assert "部门" in small_table
    assert "engineering" in small_table
    assert "员工数" in small_table


def test_source_sql_patches_use_half_open_year_range_and_correct_demo_queries():
    generator = SQLGenerator()

    sqlite_sql = generator._apply_source_sql_patches(
        "SELECT d.id, SUM(s.amount) FROM departments d JOIN sales s ON d.id = s.employee_id "
        "WHERE STRFTIME('%Y', s.sale_date) = '2026' GROUP BY d.id",
        "\u7edf\u8ba1 2026 \u5e74\u5404\u90e8\u95e8\u9500\u552e\u989d\u603b\u548c\u3002",
        "sqlite_demo",
    )
    assert "JOIN employees e ON e.id = s.employee_id" in sqlite_sql
    assert "s.sale_date >= '2026-01-01'" in sqlite_sql
    assert "s.sale_date < '2027-01-01'" in sqlite_sql
    assert "STRFTIME" not in sqlite_sql

    generic_sql = generator._apply_source_sql_patches(
        "SELECT * FROM orders WHERE YEAR(order_date) = 2024",
        "\u67e5\u8be2 2024 \u5e74\u8ba2\u5355\u3002",
        "gauss_ecommerce",
    )
    assert "order_date >= '2024-01-01'" in generic_sql
    assert "order_date < '2025-01-01'" in generic_sql

    residents_sql = generator._apply_source_sql_patches(
        "SELECT 1", "\u548c\u5e73\u91cc\u5c0f\u533a\u5f53\u524d\u767b\u8bb0\u4e86\u591a\u5c11\u540d\u5c45\u4f4f\u4eba\u5458\uff1f", "mysql_police_address"
    )
    assert "COUNT(DISTINCT person_code)" in residents_sql
    assert "house_relation_name" not in residents_sql

    event_sql = generator._apply_source_sql_patches(
        "SELECT 1", "\u627e\u51fa\u5df2\u7ed3\u6848\u4f46\u5c1a\u672a\u5173\u8054\u6848\u4e8b\u4ef6\u7684\u62a5\u8b66\uff0c\u7528\u4e8e\u6570\u636e\u5b8c\u6574\u6027\u6838\u67e5\u3002", "mysql_police_address"
    )
    assert "LEFT JOIN alert_event e ON e.alert_no = a.alert_no" in event_sql
    assert "a.alert_status_code = 'CLOSED'" in event_sql


def test_demo_patches_cover_police_stock_hadoop_and_translated_summary():
    generator = SQLGenerator()

    police_sql = generator._apply_source_sql_patches(
        "SELECT 1", "2026 年 1 月东城区已结案的治安报警有多少起？", "mysql_police_address"
    )
    assert "FROM v_nl2sql_alert_detail" in police_sql
    assert "alert_type_code = 'SECURITY'" in police_sql
    assert "alert_status_code = 'CLOSED'" in police_sql

    stock_sql = generator._apply_source_sql_patches(
        "SELECT 1", "找出最新收盘价高于自身 2026 年 7 月平均收盘价的科技股。", "postgres_stock"
    )
    assert "JOIN stock_symbols s" in stock_sql
    assert "s.sector_code = 'TECHNOLOGY'" in stock_sql

    hadoop_sql = generator._apply_source_sql_patches(
        "SELECT 1", "找出销售额高于所有城市平均销售额的城市。", "hive_hadoop_demo"
    )
    assert "WITH city_sales AS" in hadoop_sql
    assert "AVG(city_gmv)" in hadoop_sql

    summary = generator._try_template_summary(
        "查看部门人数", ["department_name", "employee_count"], [{"department_name": "技术部", "employee_count": 3}]
    )
    assert "部门为 技术部" in summary
    assert "员工数为 3" in summary


def test_prompt_removes_cot_requirement_for_general_model():
    prompt = PromptBuilder().build_prompt(
        "统计每个部门分别有多少名员工。",
        {
            "total_tables": 1,
            "tables": [
                {
                    "name": "employees",
                    "comment": "员工表",
                    "columns": [{"name": "id", "type": "INTEGER", "comment": "员工ID"}],
                    "foreign_keys": [],
                }
            ],
        },
        relevant_tables=["employees"],
        data_source="sqlite_demo",
    )

    assert "请一步步思考" not in prompt
    assert "先用中文简要说明" not in prompt
    assert "只输出 SQL" in prompt


def test_debug_output_flag_hides_client_only_fields(monkeypatch):
    response = ChatResponse(
        success=True,
        question="统计数量",
        sql="SELECT COUNT(*) FROM employees;",
        results=[{"数量": 3}],
        columns=["数量"],
        row_count=1,
        execution_time=1.23,
        llm_thought="thought",
        insight="共返回 1 条记录。",
        corrected_sql="SELECT 1;",
        stage_timings={"metadata": 0.1},
        answer="数量为 3。",
    )

    monkeypatch.setattr(settings, "nl2sql_debug_output", False)
    hidden = response_for_client(response)

    assert hidden.sql is None
    assert hidden.llm_thought is None
    assert hidden.execution_time == 1.23
    assert hidden.stage_timings == {"metadata": 0.1}
    assert hidden.answer == "数量为 3。"
    assert hidden.results == [{"数量": 3}]

    monkeypatch.setattr(settings, "nl2sql_debug_output", True)
    visible = response_for_client(response)
    assert visible.sql == "SELECT COUNT(*) FROM employees;"
    assert visible.stage_timings == {"metadata": 0.1}


def test_postgres_metadata_cache_status_and_clear():
    adapter = PostgreSQLAdapter(
        name="pg_test",
        host="127.0.0.1",
        port=5432,
        user="u",
        password="p",
        database="d",
    )

    assert adapter.metadata_cache_status()["supported"] is True
    assert adapter.metadata_cache_status()["cached"] is False
    assert adapter.clear_metadata_cache() is True
    assert adapter.metadata_cache_status()["cached"] is False


def test_audit_db_path_is_partitioned_by_date():
    path = audit_db_path("2026-07-28")
    assert path.name == "audit_2026-07-28.db"
    assert path.parent.name == "2026-07-28"
    assert path.parent.parent.name == "audit"


def test_audit_result_sample_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "audit_result_sample_rows", 2)
    columns, sample, truncated = prepare_result_sample(
        ["id"],
        [{"id": 1}, {"id": 2}, {"id": 3}],
    )

    assert columns == ["id"]
    assert sample == [{"id": 1}, {"id": 2}]
    assert truncated is True
