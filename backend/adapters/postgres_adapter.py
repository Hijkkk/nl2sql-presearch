"""
PostgreSQL / 高斯 数据源适配器

高斯常见部署兼容 PostgreSQL 协议，MVP 阶段复用该适配器。
"""
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .base import BaseDataSourceAdapter


class PostgreSQLAdapter(BaseDataSourceAdapter):
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        schema: str = "public",
    ):
        super().__init__(name)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.schema = schema or "public"
        self._conn = None

    def get_dialect(self) -> str:
        return "postgres"

    def _get_connection(self):
        if self._conn is None or getattr(self._conn, "closed", 1):
            if not self.database:
                raise ValueError(f"{self.name} 未配置数据库名")
            try:
                import psycopg2
                import psycopg2.extras
            except ImportError as exc:
                raise RuntimeError("PostgreSQL/高斯 数据源需要安装 psycopg2-binary") from exc

            self._conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                dbname=self.database,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            self._conn.autocommit = True
        return self._conn

    def ping(self) -> None:
        conn = self._get_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")

    def get_metadata(self) -> Dict[str, Any]:
        conn = self._get_connection()
        tables: List[Dict[str, Any]] = []

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """,
                (self.schema,),
            )
            table_rows = cursor.fetchall()

            for row in table_rows:
                table_name = row["table_name"]
                cursor.execute(
                    """
                    SELECT
                        column_name,
                        data_type,
                        udt_name,
                        is_nullable,
                        column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (self.schema, table_name),
                )
                column_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                      AND tc.table_schema = %s
                      AND tc.table_name = %s
                    ORDER BY kcu.ordinal_position
                    """,
                    (self.schema, table_name),
                )
                primary_key = [item["column_name"] for item in cursor.fetchall()]

                cursor.execute(
                    """
                    SELECT
                        kcu.column_name,
                        ccu.table_name AS ref_table,
                        ccu.column_name AS ref_column
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema = %s
                      AND tc.table_name = %s
                    """,
                    (self.schema, table_name),
                )
                foreign_keys = [
                    {
                        "column": item["column_name"],
                        "ref_table": item["ref_table"],
                        "ref_column": item["ref_column"],
                    }
                    for item in cursor.fetchall()
                ]

                columns = [
                    {
                        "name": item["column_name"],
                        "type": item["udt_name"] or item["data_type"],
                        "comment": "",
                        "not_null": item["is_nullable"] == "NO",
                        "default": item["column_default"],
                        "pk": item["column_name"] in primary_key,
                    }
                    for item in column_rows
                ]

                tables.append(
                    {
                        "name": table_name,
                        "comment": f"{self.schema}.{table_name}",
                        "columns": columns,
                        "primary_key": primary_key,
                        "foreign_keys": foreign_keys,
                    }
                )

        return {"tables": tables, "total_tables": len(tables)}

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                columns = [desc.name for desc in cursor.description] if cursor.description else []
                return [dict(row) for row in rows], columns
        except Exception as exc:
            logger.error(f"PostgreSQL/Gauss query error: {exc}\nSQL: {sql}")
            raise

    def close(self) -> None:
        if self._conn is not None and not getattr(self._conn, "closed", 1):
            self._conn.close()
