"""NL2SQL audit logging with daily SQLite files."""
import sqlite3
import json
from datetime import date, datetime
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
    """写入一条审计日志"""
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
            user,
            question,
            generated_sql,
            executed_sql,
            data_source,
            row_count,
            status,
            error_message,
            execution_time,
            int(rag_enabled),
            json.dumps(rag_hits or [], ensure_ascii=False),
            json.dumps(selected_tables or [], ensure_ascii=False),
            max((hit.get("score", 0) for hit in (rag_hits or [])), default=None),
            None if query_guard_passed is None else int(query_guard_passed),
            prompt_token_estimate,
            json.dumps(stage_timings or {}, ensure_ascii=False),
            model_id,
            raw_model_output,
            llm_thought,
            prompt_template,
            None if generation_cache_hit is None else int(generation_cache_hit),
            int(correction_attempted),
            corrected_sql,
            json.dumps(result_columns or [], ensure_ascii=False, default=str),
            json.dumps(result_sample or [], ensure_ascii=False, default=str),
            int(result_truncated),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")


def log_agent_trace(
    *,
    request_id: str,
    question: str,
    status: str,
    events: list[dict[str, Any]],
    route_mode: Optional[str] = None,
    final_plan: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    """Append a controlled-agent trace without modifying historical audit rows."""
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
        payload = {
            "trace_version": 2,
            "created_at": created_at.isoformat(),
            "request_id": request_id,
            "question": question,
            "status": status,
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
            "plan": plan,
            "validation": validation,
            "review": review,
            "execution": execution,
            "answer": answer,
            "events": events,
            "error": error,
            "privacy": {"raw_result_rows_included": False, "credentials_included": False},
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
