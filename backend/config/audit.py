"""
简单审计日志模块
把每次查询记录写入 SQLite audit.db
"""
import sqlite3
from datetime import datetime
from typing import Optional
from loguru import logger
from backend.config.config import settings
import os


def init_audit_db():
    """初始化审计表"""
    os.makedirs(os.path.dirname(settings.audit_db_path), exist_ok=True)
    conn = sqlite3.connect(settings.audit_db_path)
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
            execution_time REAL
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Audit database ready at {settings.audit_db_path}")


def log_audit(
    question: str,
    generated_sql: str,
    executed_sql: str,
    data_source: str,
    row_count: int,
    status: str,
    error_message: Optional[str] = None,
    execution_time: float = 0.0,
    user: str = "demo_user"
):
    """写入一条审计日志"""
    try:
        conn = sqlite3.connect(settings.audit_db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO audit_logs 
            (timestamp, user, question, generated_sql, executed_sql, data_source, 
             row_count, status, error_message, execution_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            execution_time
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")
