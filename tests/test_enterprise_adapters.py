import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.adapters.dameng_adapter import DamengAdapter
from backend.adapters.hive_adapter import HiveAdapter
from backend.adapters.postgres_adapter import PostgreSQLAdapter


def test_postgres_adapter_uses_postgres_dialect():
    adapter = PostgreSQLAdapter(
        name="postgres_local",
        host="127.0.0.1",
        port=5432,
        user="postgres",
        password="",
        database="demo",
    )

    assert adapter.get_dialect() == "postgres"


def test_hive_adapter_uses_hive_dialect():
    adapter = HiveAdapter(
        name="hive_local",
        host="127.0.0.1",
        port=10000,
        database="default",
    )

    assert adapter.get_dialect() == "hive"


def test_dameng_adapter_uses_oracle_dialect():
    adapter = DamengAdapter(
        name="dameng_local",
        host="127.0.0.1",
        port=5236,
        user="SYSDBA",
        password="",
    )

    assert adapter.get_dialect() == "oracle"


def test_missing_postgres_driver_has_readable_error(monkeypatch):
    adapter = PostgreSQLAdapter(
        name="postgres_local",
        host="127.0.0.1",
        port=5432,
        user="postgres",
        password="",
        database="demo",
    )

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "psycopg2":
            raise ImportError("missing psycopg2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError, match="psycopg2-binary"):
        adapter._get_connection()
