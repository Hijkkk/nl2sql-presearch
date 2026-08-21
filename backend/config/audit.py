"""NL2SQL audit logging with daily SQLite files."""
import sqlite3
import json
from datetime import date, datetime
from dataclasses import dataclass, field
from typing import Any, Optional
from loguru import logger
from backend.config.config import settings
import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalize_audit_date(audit_date: str | date | None = None) -> str:
    if audit_date is None:
        return date.today().isoformat()
    if isinstance(audit_date, date):
        return audit_date.isoformat()
    datetime.strptime(audit_date, "%Y-%m-%d")
    return audit_date


def audit_root_dir() -> Path:
    """Resolve the root directory that contains daily audit folders."""
    configured = Path(settings.audit_db_path)
    if configured.is_absolute():
        base = configured.parent
    else:
        base = project_root() / configured.parent
    return base / "audit"


def audit_db_path(audit_date: str | date | None = None) -> Path:
    """Return daily audit DB path: data/audit/YYYY-MM-DD/audit_YYYY-MM-DD.db."""
    day = _normalize_audit_date(audit_date)
    return audit_root_dir() / day / f"audit_{day}.db"


def agent_trace_root_dir() -> Path:
    """Human-readable, per-run Agent traces kept separately from SQLite audit rows."""
    return project_root() / "data" / "agent_traces"


def init_audit_db(audit_date: str | date | None = None):
    """初始化审计表"""
    path = audit_db_path(audit_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user TEXT DEFAULT 'demo_user',
            question TEXT,
            generated_sql TEXT,
            executed_sql TEXT,
            data_source TEXT,
            row_count INTEGER DEFAULT 0,
            status TEXT,  -- success / failed / blocked
            error_message TEXT,
            execution_time REAL,
            rag_enabled INTEGER DEFAULT 0,
            rag_hits_json TEXT DEFAULT '[]',
            selected_tables_json TEXT DEFAULT '[]',
            rag_top_score REAL,
            query_guard_passed INTEGER,
            prompt_token_estimate INTEGER,
            stage_timings_json TEXT DEFAULT '{}',
            model_id TEXT,
            raw_model_output TEXT,
            llm_thought TEXT,
            prompt_template TEXT,
            generation_cache_hit INTEGER,
            correction_attempted INTEGER DEFAULT 0,
            corrected_sql TEXT,
            result_columns_json TEXT DEFAULT '[]',
            result_sample_json TEXT DEFAULT '[]',
            result_truncated INTEGER DEFAULT 0
        )
    """)
    # Agent records are additive: audit_logs remains the historical fact table
    # for the original chat path, while these tables preserve graph structure.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            request_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            question TEXT NOT NULL,
            status TEXT NOT NULL,
            route_mode TEXT,
            final_plan_json TEXT DEFAULT '{}',
            error_message TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            node_name TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT DEFAULT '{}',
            FOREIGN KEY(request_id) REFERENCES agent_runs(request_id)
        )
    """)
    existing_columns = {row[1] for row in cursor.execute("PRAGMA table_info(audit_logs)")}
    migrations = {
        "rag_enabled": "INTEGER DEFAULT 0",
        "rag_hits_json": "TEXT DEFAULT '[]'",
        "selected_tables_json": "TEXT DEFAULT '[]'",
        "rag_top_score": "REAL",
        "query_guard_passed": "INTEGER",
        "prompt_token_estimate": "INTEGER",
        "stage_timings_json": "TEXT DEFAULT '{}'",
        "model_id": "TEXT",
        "raw_model_output": "TEXT",
        "llm_thought": "TEXT",
        "prompt_template": "TEXT",
        "generation_cache_hit": "INTEGER",
        "correction_attempted": "INTEGER DEFAULT 0",
        "corrected_sql": "TEXT",
        "result_columns_json": "TEXT DEFAULT '[]'",
        "result_sample_json": "TEXT DEFAULT '[]'",
        "result_truncated": "INTEGER DEFAULT 0",
    }
    for name, definition in migrations.items():
        if name not in existing_columns:
            cursor.execute(f"ALTER TABLE audit_logs ADD COLUMN {name} {definition}")
    conn.commit()
    conn.close()
    logger.info(f"Audit database ready at {path}")


def prepare_result_sample(
    columns: Optional[list[Any]] = None,
    results: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[Any], list[dict[str, Any]], bool]:
    """Prepare a bounded result sample for audit storage."""
    rows = results or []
    sample_size = max(0, int(settings.audit_result_sample_rows))
    sample = rows[:sample_size] if sample_size else []
    return columns or [], sample, len(rows) > len(sample)


@dataclass(slots=True)
class AuditRecord:
    """聊天和受控代理执行共享的共同审计事实。
    仅代理的图表详情保留在 ``agent_runs`` / ``agent_steps`` 和
    JSON 跟踪中。此记录特意镜像了 ``audit_logs`` 中的列，
    以便可以使用同一份报告或 SQL 语句查询这两个路径。
    """

    question: str
    generated_sql: str
    executed_sql: str
    data_source: str
    row_count: int
    status: str
    error_message: Optional[str] = None
    execution_time: float = 0.0
    user: str = "demo_user"
    rag_enabled: bool = False
    rag_hits: list[dict[str, Any]] = field(default_factory=list)
    selected_tables: list[str] = field(default_factory=list)
    query_guard_passed: Optional[bool] = None
    prompt_token_estimate: Optional[int] = None
    stage_timings: dict[str, float] = field(default_factory=dict)
    model_id: Optional[str] = None
    raw_model_output: Optional[str] = None
    llm_thought: Optional[str] = None
    prompt_template: Optional[str] = None
    generation_cache_hit: Optional[bool] = None
    correction_attempted: bool = False
    corrected_sql: Optional[str] = None
    result_columns: list[Any] = field(default_factory=list)
    result_sample: list[dict[str, Any]] = field(default_factory=list)
    result_truncated: bool = False


def write_audit_record(record: AuditRecord) -> None:
    """对于任何执行路径，保留一条规范化的审计记录。"""
    try:
        init_audit_db()
        conn = sqlite3.connect(audit_db_path())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO audit_logs
            (timestamp, user, question, generated_sql, executed_sql, data_source,
             row_count, status, error_message, execution_time, rag_enabled, rag_hits_json,
             selected_tables_json, rag_top_score, query_guard_passed, prompt_token_estimate, stage_timings_json,
             model_id, raw_model_output, llm_thought, prompt_template, generation_cache_hit, correction_attempted, corrected_sql,
             result_columns_json, result_sample_json, result_truncated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            record.user,
            record.question,
            record.generated_sql,
            record.executed_sql,
            record.data_source,
            record.row_count,
            record.status,
            record.error_message,
            record.execution_time,
            int(record.rag_enabled),
            json.dumps(record.rag_hits, ensure_ascii=False),
            json.dumps(record.selected_tables, ensure_ascii=False),
            max((hit.get("score", 0) for hit in record.rag_hits), default=None),
            None if record.query_guard_passed is None else int(record.query_guard_passed),
            record.prompt_token_estimate,
            json.dumps(record.stage_timings, ensure_ascii=False),
            record.model_id,
            record.raw_model_output,
            record.llm_thought,
            record.prompt_template,
            None if record.generation_cache_hit is None else int(record.generation_cache_hit),
            int(record.correction_attempted),
            record.corrected_sql,
            json.dumps(record.result_columns, ensure_ascii=False, default=str),
            json.dumps(record.result_sample, ensure_ascii=False, default=str),
            int(record.result_truncated),
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error(f"Failed to write audit log: {exc}")


def log_audit(
    question: str,
    generated_sql: str,
    executed_sql: str,
    data_source: str,
    row_count: int,
    status: str,
    error_message: Optional[str] = None,
    execution_time: float = 0.0,
    user: str = "demo_user",
    rag_enabled: bool = False,
    rag_hits: Optional[list[dict]] = None,
    selected_tables: Optional[list[str]] = None,
    query_guard_passed: Optional[bool] = None,
    prompt_token_estimate: Optional[int] = None,
    stage_timings: Optional[dict[str, float]] = None,
    model_id: Optional[str] = None,
    raw_model_output: Optional[str] = None,
    llm_thought: Optional[str] = None,
    prompt_template: Optional[str] = None,
    generation_cache_hit: Optional[bool] = None,
    correction_attempted: bool = False,
    corrected_sql: Optional[str] = None,
    result_columns: Optional[list[Any]] = None,
    result_sample: Optional[list[dict[str, Any]]] = None,
    result_truncated: bool = False,
):
    """Backward-compatible Chat facade over the shared audit writer."""
    write_audit_record(AuditRecord(
        question=question,
        generated_sql=generated_sql,
        executed_sql=executed_sql,
        data_source=data_source,
        row_count=row_count,
        status=status,
        error_message=error_message,
        execution_time=execution_time,
        user=user,
        rag_enabled=rag_enabled,
        rag_hits=rag_hits or [],
        selected_tables=selected_tables or [],
        query_guard_passed=query_guard_passed,
        prompt_token_estimate=prompt_token_estimate,
        stage_timings=stage_timings or {},
        model_id=model_id,
        raw_model_output=raw_model_output,
        llm_thought=llm_thought,
        prompt_template=prompt_template,
        generation_cache_hit=generation_cache_hit,
        correction_attempted=correction_attempted,
        corrected_sql=corrected_sql,
        result_columns=result_columns or [],
        result_sample=result_sample or [],
        result_truncated=result_truncated,
    ))


def _agent_query_guard_outcome(execution: dict[str, Any]) -> Optional[bool]:
    """Return a precise QueryGuard result without claiming other checks are Guard failures."""
    if execution.get("success"):
        return True
    error = str(execution.get("error") or "")
    return False if "QUERY_GUARD" in error else None


def _agent_audit_record(
    *,
    question: str,
    final_plan: dict[str, Any],
    execution: dict[str, Any],
    execution_time: float,
    stage_timings: dict[str, float],
    model_id: Optional[str],
    error_message: Optional[str],
    contexts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> AuditRecord:
    task = (final_plan.get("subtasks") or [{}])[0]
    source_id = str(task.get("source_id") or "")
    context = next((item for item in contexts if (item.get("source") or {}).get("source_id") == source_id), {})
    generation = execution.get("generation_trace") or {}
    if not isinstance(generation, dict):
        generation = {}
    columns = execution.get("columns") or []
    results = execution.get("results") or []
    result_columns, result_sample, result_truncated = prepare_result_sample(columns, results)
    rag_hits = [
        {
            "source_id": candidate.get("source_id"),
            "score": candidate.get("hybrid_score")
            if candidate.get("hybrid_score") is not None
            else candidate.get("score", 0),
        }
        for candidate in candidates
    ]
    return AuditRecord(
        question=question,
        generated_sql=str(execution.get("sql") or ""),
        executed_sql=str(execution.get("sql") or "") if execution.get("success") else "",
        data_source=source_id,
        row_count=int(execution.get("row_count") or 0),
        status="success" if execution.get("success") else "blocked",
        error_message=error_message or execution.get("error"),
        execution_time=execution_time,
        rag_enabled=bool(settings.agent_vector_enabled),
        rag_hits=rag_hits,
        selected_tables=list(context.get("schema_closure_object_ids") or task.get("object_ids") or []),
        query_guard_passed=_agent_query_guard_outcome(execution),
        prompt_token_estimate=generation.get("prompt_token_estimate"),
        stage_timings=stage_timings,
        model_id=model_id,
        raw_model_output=generation.get("raw_model_output") or "[controlled_agent:no_sql_output]",
        llm_thought=generation.get("llm_thought") or "[受控 Agent：详见 agent_runs / agent_steps]",
        prompt_template=generation.get("prompt_template") or None,
        generation_cache_hit=bool(generation.get("generation_cache_hit")),
        correction_attempted=bool(execution.get("retry_attempted")),
        result_columns=result_columns,
        result_sample=result_sample,
        result_truncated=result_truncated,
    )


def log_agent_trace(
    *,
    request_id: str,
    question: str,
    status: str,
    events: list[dict[str, Any]],
    route_mode: Optional[str] = None,
    final_plan: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    execution: Optional[dict[str, Any]] = None,
    execution_time: float = 0.0,
    stage_timings: Optional[dict[str, float]] = None,
    model_id: Optional[str] = None,
    candidates: Optional[list[dict[str, Any]]] = None,
    contexts: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Persist Agent-specific detail plus the same normalized audit fact as Chat."""
    try:
        init_audit_db()
        conn = sqlite3.connect(audit_db_path())
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_runs (request_id, timestamp, question, status, route_mode, final_plan_json, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                datetime.now().isoformat(),
                question,
                status,
                route_mode,
                json.dumps(final_plan or {}, ensure_ascii=False),
                error_message,
            ),
        )
        cursor.executemany(
            """
            INSERT INTO agent_steps (request_id, step_index, node_name, status, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    request_id,
                    index,
                    str(event.get("node", "unknown")),
                    str(event.get("status", "unknown")),
                    json.dumps(event, ensure_ascii=False, default=str),
                )
                for index, event in enumerate(events, start=1)
            ],
        )
        conn.commit()
        conn.close()
        execution_payload = execution or {}
        write_audit_record(_agent_audit_record(
            question=question,
            final_plan=final_plan or {},
            execution=execution_payload,
            execution_time=execution_time,
            stage_timings=stage_timings or {"total": execution_time},
            model_id=model_id,
            error_message=error_message,
            contexts=contexts or [],
            candidates=candidates or [],
        ))
    except Exception as exc:
        logger.error(f"Failed to write controlled-agent audit trace: {exc}")


def write_agent_execution_trace(
    *,
    request_id: str,
    question: str,
    status: str,
    candidates: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
    plan: Optional[dict[str, Any]],
    validation: Optional[dict[str, Any]],
    review: Optional[dict[str, Any]],
    execution: Optional[dict[str, Any]],
    answer: Optional[str],
    events: list[dict[str, Any]],
    error: Optional[str],
    stage_timings: Optional[dict[str, float]] = None,
) -> None:
    """Write a safe, inspectable JSON trace for one controlled-agent request.

    The trace records routing, approved schema scope, reviewer decisions, SQL and
    execution metadata.  It intentionally excludes credentials and raw result
    rows; the latter may contain business-sensitive data and remain in the
    governed source/audit sampling path.
    """
    try:
        created_at = datetime.now()
        directory = agent_trace_root_dir() / created_at.date().isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        # Keep the high-level decision record first. Retrieval metadata and the
        # full XiYan prompt can be large, so put both at the end of the file
        # where an operator can open them only when needed.
        execution_summary = dict(execution or {})
        xiyan_generation = _format_trace_generation(execution_summary.pop("generation_trace", None))
        event_summaries: list[dict[str, Any]] = []
        for event in events:
            event_summary = dict(event)
            if "generation" in event_summary:
                event_summary["generation_recorded_in"] = "xiyan_sql_generation"
                event_summary.pop("generation", None)
            event_summaries.append(event_summary)

        payload = {
            "trace_version": 4,
            "created_at": created_at.isoformat(),
            "request_id": request_id,
            "question": question,
            "status": status,
            "plan": plan,
            "validation": validation,
            "review": review,
            "execution": execution_summary,
            "answer": answer,
            "events": event_summaries,
            "stage_timings_seconds": stage_timings or {},
            "error": error,
            "privacy": {"raw_result_rows_included": False, "credentials_included": False},
            "retrieval": {
                "pipeline": {
                    "lexical_recall": "关键词与中文二元词召回，作为确定性回退和混合重排基础。",
                    "embedding": {
                        "enabled": bool(settings.agent_vector_enabled),
                        "model": settings.agent_embedding_model,
                        "dimensions": settings.agent_embedding_dimensions,
                        "raw_query_vector_stored": False,
                    },
                    "vector_search": {
                        "collection": settings.agent_qdrant_collection,
                        "distance": "cosine",
                        "payload_only": True,
                    },
                    "hybrid_ranking": {
                        "formula_when_semantic_hit": "semantic_score * 10 + lexical_score",
                        "deterministic_seed_count": 2,
                        "schema_closure": "按关系补齐 JOIN 依赖，随后仅把受限对象交给规划与 SQL 生成。",
                    },
                },
                "candidates": candidates,
                "schema_contexts": contexts,
            },
            "xiyan_sql_generation": xiyan_generation,
        }
        # Timestamp-first names are easy to inspect chronologically. Microseconds
        # keep one trace file per concurrent request; request_id stays in JSON.
        filename = created_at.strftime("%Y-%m-%d_%H-%M-%S-%f") + ".json"
        path = directory / filename
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)
    except Exception as exc:
        logger.error(f"Failed to write controlled-agent execution trace: {exc}")


def _format_trace_generation(generation: Any) -> Any:
    """Make the large XiYan prompt readable in JSON without changing audit text.

    JSON cannot contain literal line breaks in a string value.  Persisting the
    prompt as a line array keeps the trace valid JSON and makes it readable in
    editors, while SQLite ``audit_logs.prompt_template`` retains the original
    text for existing tools and SQL consumers.
    """
    if not isinstance(generation, dict):
        return generation
    formatted = dict(generation)
    prompt = formatted.get("prompt_template")
    if isinstance(prompt, str):
        formatted["prompt_template"] = prompt.splitlines()
        formatted["prompt_template_format"] = "lines"
    return formatted


def prompt_template_to_text(prompt_template: Any) -> str | None:
    """Read either the legacy prompt string or the formatted Agent-trace value."""
    if isinstance(prompt_template, str):
        return prompt_template
    if isinstance(prompt_template, list) and all(isinstance(line, str) for line in prompt_template):
        return "\n".join(prompt_template)
    return None
