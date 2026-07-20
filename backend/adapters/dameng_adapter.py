"""
达梦数据库数据源适配器

依赖达梦官方 Python 驱动 dmPython。达梦 SQL 与 Oracle 兼容度较高，
MVP 阶段在 SQL 安全解析中使用 oracle 方言。
"""
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .base import BaseDataSourceAdapter


class DamengAdapter(BaseDataSourceAdapter):
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        user: str,
        password: str,
        schema: str = "",
    ):
        super().__init__(name)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.schema = (schema or user or "").upper()
        self._conn = None

    def get_dialect(self) -> str:
        return "oracle"

    def _get_connection(self):
        if self._conn is None:
            try:
                import dmPython
            except ImportError as exc:
                raise RuntimeError("达梦数据源需要安装达梦官方 dmPython 驱动") from exc

            self._conn = dmPython.connect(
                user=self.user,
                password=self.password,
                server=self.host,
                port=self.port,
            )
        return self._conn

    def ping(self) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchall()
        finally:
            cursor.close()

    def get_metadata(self) -> Dict[str, Any]:
        conn = self._get_connection()
        cursor = conn.cursor()
        tables: List[Dict[str, Any]] = []
        try:
            cursor.execute(
                """
                SELECT table_name, comments
                FROM all_tab_comments
                WHERE owner = ? AND table_type = 'TABLE'
                ORDER BY table_name
                """,
                (self.schema,),
            )
            table_rows = cursor.fetchall()

            for table_name, table_comment in table_rows:
                cursor.execute(
                    """
                    SELECT
                        c.column_name,
                        c.data_type,
                        c.nullable,
                        cc.comments
                    FROM all_tab_columns c
                    LEFT JOIN all_col_comments cc
                      ON cc.owner = c.owner
                     AND cc.table_name = c.table_name
                     AND cc.column_name = c.column_name
                    WHERE c.owner = ? AND c.table_name = ?
                    ORDER BY c.column_id
                    """,
                    (self.schema, table_name),
                )
                column_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT cols.column_name
                    FROM all_constraints cons
                    JOIN all_cons_columns cols
                      ON cons.owner = cols.owner
                     AND cons.constraint_name = cols.constraint_name
                    WHERE cons.constraint_type = 'P'
                      AND cons.owner = ?
                      AND cons.table_name = ?
                    ORDER BY cols.position
                    """,
                    (self.schema, table_name),
                )
                primary_key = [row[0] for row in cursor.fetchall()]

                cursor.execute(
                    """
                    SELECT
                        cols.column_name,
                        ref_cols.table_name AS ref_table,
                        ref_cols.column_name AS ref_column
                    FROM all_constraints cons
                    JOIN all_cons_columns cols
                      ON cons.owner = cols.owner
                     AND cons.constraint_name = cols.constraint_name
                    JOIN all_cons_columns ref_cols
                      ON cons.r_owner = ref_cols.owner
                     AND cons.r_constraint_name = ref_cols.constraint_name
                     AND cols.position = ref_cols.position
                    WHERE cons.constraint_type = 'R'
                      AND cons.owner = ?
                      AND cons.table_name = ?
                    """,
                    (self.schema, table_name),
                )
                foreign_keys = [
                    {"column": row[0], "ref_table": row[1], "ref_column": row[2]}
                    for row in cursor.fetchall()
                ]

                columns = [
                    {
                        "name": row[0],
                        "type": row[1],
                        "comment": row[3] or "",
                        "not_null": row[2] == "N",
                        "default": None,
                        "pk": row[0] in primary_key,
                    }
                    for row in column_rows
                ]

                tables.append(
                    {
                        "name": table_name,
                        "comment": table_comment or "",
                        "columns": columns,
                        "primary_key": primary_key,
                        "foreign_keys": foreign_keys,
                    }
                )
        finally:
            cursor.close()

        return {"tables": tables, "total_tables": len(tables)}

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params or ())
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in rows], columns
        except Exception as exc:
            logger.error(f"Dameng query error: {exc}\nSQL: {sql}")
            raise
        finally:
            cursor.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
