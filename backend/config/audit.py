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
            generation_cache_hit INTEGER,
            correction_attempted INTEGER DEFAULT 0,
            corrected_sql TEXT,
            result_columns_json TEXT DEFAULT '[]',
            result_sample_json TEXT DEFAULT '[]',
            result_truncated INTEGER DEFAULT 0
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
             model_id, raw_model_output, llm_thought, generation_cache_hit, correction_attempted, corrected_sql,
             result_columns_json, result_sample_json, result_truncated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
