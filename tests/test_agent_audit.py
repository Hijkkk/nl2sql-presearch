import sqlite3
import json

from backend.config import audit
from backend.crud.conversation import _AGENT_HISTORY_MAX_BYTES, _history_safe_agent_presentation


def test_agent_trace_also_creates_a_queryable_audit_log_row(monkeypatch, tmp_path):
    database_path = tmp_path / "audit.db"
    monkeypatch.setattr(audit, "audit_db_path", lambda _date=None: database_path)

    audit.log_agent_trace(
        request_id="agent-audit-test",
        question="统计部门员工数",
        status="executed_summarized",
        route_mode="single_source",
        final_plan={"subtasks": [{"source_id": "sqlite_demo"}]},
        execution={
            "success": True,
            "sql": "SELECT COUNT(*) AS employee_count FROM employees",
            "columns": ["employee_count"],
            "results": [{"employee_count": 3}],
            "row_count": 1,
        },
        execution_time=1.25,
        stage_timings={"total": 1.25},
        model_id="xiyan-sql-3b-finetune",
        events=[{"node": "execute", "status": "executed_success"}],
    )

    connection = sqlite3.connect(database_path)
    agent_run = connection.execute("SELECT status FROM agent_runs WHERE request_id = ?", ("agent-audit-test",)).fetchone()
    audit_row = connection.execute(
        "SELECT data_source, status, execution_time, model_id, raw_model_output, selected_tables_json FROM audit_logs"
    ).fetchone()
    connection.close()

    assert agent_run == ("executed_summarized",)
    assert audit_row == (
        "sqlite_demo", "success", 1.25, "xiyan-sql-3b-finetune",
        "[controlled_agent:no_sql_output]", "[]",
    )


def test_history_presentation_excludes_full_generation_trace_and_stays_bounded():
    presentation = {
        "is_agent": True,
        "status": "executed_summarized",
        "execution": {"success": True, "generation_trace": {"prompt_template": "p" * 70000}},
        "events": [{"node": "execute", "generation": {"raw_model_output": "o" * 70000}}],
    }

    compacted = _history_safe_agent_presentation(presentation)
    encoded_size = len(__import__("json").dumps(compacted, ensure_ascii=False).encode("utf-8"))

    assert compacted["execution"]["generation_trace"] == "[仅保存在 Agent JSON 审计轨迹中]"
    assert compacted["events"][0]["generation"] == "[仅保存在 Agent JSON 审计轨迹中]"
    assert encoded_size <= _AGENT_HISTORY_MAX_BYTES


def test_agent_json_trace_puts_retrieval_and_xiyan_prompt_at_the_end(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "agent_trace_root_dir", lambda: tmp_path)

    audit.write_agent_execution_trace(
        request_id="trace-order-test",
        question="测试问题",
        status="executed_summarized",
        candidates=[{"source_id": "sqlite_demo"}],
        contexts=[{"source": {"source_id": "sqlite_demo"}}],
        plan={"route_mode": "single_source"},
        validation={"valid": True},
        review={"approved": True},
        execution={
            "success": True,
            "generation_trace": {"prompt_template": "完整 XiYan Prompt", "prompt_token_estimate": 100},
        },
        answer="完成",
        events=[{"node": "execute", "generation": {"prompt_template": "完整 XiYan Prompt"}}],
        error=None,
    )

    trace_path = next(tmp_path.rglob("*.json"))
    payload = json.loads(trace_path.read_text(encoding="utf-8"))

    assert list(payload)[-2:] == ["retrieval", "xiyan_sql_generation"]
    assert "generation_trace" not in payload["execution"]
    assert payload["xiyan_sql_generation"]["prompt_template"] == ["完整 XiYan Prompt"]
    assert payload["xiyan_sql_generation"]["prompt_template_format"] == "lines"
    assert payload["events"][0]["generation_recorded_in"] == "xiyan_sql_generation"


def test_agent_audit_record_reuses_chat_fields_from_generation_trace(monkeypatch, tmp_path):
    database_path = tmp_path / "audit.db"
    monkeypatch.setattr(audit, "audit_db_path", lambda _date=None: database_path)

    audit.log_agent_trace(
        request_id="agent-audit-fields",
        question="统计部门员工数",
        status="executed_summarized",
        route_mode="single_source",
        final_plan={"subtasks": [{"source_id": "sqlite_demo", "object_ids": ["employees"]}]},
        execution={
            "success": True,
            "sql": "SELECT COUNT(*) FROM employees",
            "columns": ["employee_count"],
            "results": [{"employee_count": 3}],
            "row_count": 1,
            "generation_trace": {
                "prompt_template": "line one\nline two",
                "prompt_token_estimate": 42,
                "raw_model_output": "SELECT COUNT(*) FROM employees",
                "llm_thought": "统计员工数",
                "generation_cache_hit": True,
            },
        },
        execution_time=1.25,
        stage_timings={"sql_generation": 0.5, "total": 1.25},
        model_id="xiyan-sql-3b-finetune",
        events=[],
        candidates=[{"source_id": "sqlite_demo", "hybrid_score": 8.0}],
        contexts=[{"source": {"source_id": "sqlite_demo"}, "schema_closure_object_ids": ["employees", "departments"]}],
    )

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT prompt_template, prompt_token_estimate, raw_model_output, llm_thought, generation_cache_hit, "
            "selected_tables_json, result_columns_json, result_sample_json FROM audit_logs"
        ).fetchone()

    assert row == (
        "line one\nline two", 42, "SELECT COUNT(*) FROM employees", "统计员工数", 1,
        '["employees", "departments"]', '["employee_count"]', '[{"employee_count": 3}]',
    )
