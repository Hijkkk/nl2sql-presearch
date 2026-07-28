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
    assert "symbol=AAA" in topn


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
    assert hidden.execution_time is None
    assert hidden.llm_thought is None
    assert hidden.stage_timings is None
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
