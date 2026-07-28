import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.security.query_guard import QueryGuard


def assert_blocked(sql: str, dialect: str = "sqlite"):
    safe, error = QueryGuard.validate_read_only(sql, dialect)
    assert safe is False
    assert error


def test_query_guard_allows_safe_column_names_containing_keywords():
    safe, error = QueryGuard.validate_read_only(
        "SELECT updated_at, created_at FROM audit_logs",
        "sqlite",
    )

    assert safe is True
    assert error is None


def test_query_guard_blocks_write_and_ddl_statements():
    for sql in [
        "UPDATE employees SET salary = 1",
        "DELETE FROM employees WHERE id = 1",
        "DROP TABLE employees",
        "ALTER TABLE employees ADD COLUMN flag INTEGER",
        "TRUNCATE TABLE employees",
    ]:
        assert_blocked(sql)


def test_query_guard_blocks_multi_statement_payloads():
    assert_blocked("SELECT * FROM employees; SELECT * FROM departments")
    assert_blocked("SELECT * FROM employees; DROP TABLE employees")


def test_query_guard_blocks_comment_wrapped_dangerous_keywords():
    assert_blocked("SELECT * FROM employees /* DROP TABLE employees */")


def test_query_guard_allows_cte_read_only_query():
    safe, error = QueryGuard.validate_read_only(
        "WITH high_salary AS (SELECT * FROM employees WHERE salary > 10000) SELECT * FROM high_salary",
        "sqlite",
    )

    assert safe is True
    assert error is None
