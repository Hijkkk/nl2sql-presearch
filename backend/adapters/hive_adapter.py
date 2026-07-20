"""
Hive / Hadoop 数据源适配器

Hadoop 是存储和计算生态，NL2SQL 通常通过 HiveServer2 暴露 SQL 能力。
"""
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .base import BaseDataSourceAdapter


class HiveAdapter(BaseDataSourceAdapter):
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        database: str = "default",
        auth: str = "NOSASL",
    ):
        super().__init__(name)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.database = database or "default"
        self.auth = auth or "NOSASL"
        self._conn = None

    def get_dialect(self) -> str:
        return "hive"

    def _get_connection(self):
        if self._conn is None:
            try:
                from pyhive import hive
            except ImportError as exc:
                raise RuntimeError("Hive/Hadoop 数据源需要安装 PyHive[hive]") from exc

            kwargs = {
                "host": self.host,
                "port": self.port,
                "username": self.username or None,
                "database": self.database,
                "auth": self.auth,
            }
            if self.password:
                kwargs["password"] = self.password
            self._conn = hive.Connection(**kwargs)
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
            cursor.execute("SHOW TABLES")
            table_names = [row[0] for row in cursor.fetchall()]

            for table_name in table_names:
                cursor.execute(f"DESCRIBE `{table_name}`")
                describe_rows = cursor.fetchall()
                columns = []
                for row in describe_rows:
                    column_name = str(row[0]).strip()
                    if not column_name or column_name.startswith("#"):
                        continue
                    columns.append(
                        {
                            "name": column_name,
                            "type": str(row[1]).strip() if len(row) > 1 else "string",
                            "comment": str(row[2]).strip() if len(row) > 2 and row[2] else "",
                            "not_null": False,
                            "default": None,
                            "pk": False,
                        }
                    )

                tables.append(
                    {
                        "name": table_name,
                        "comment": f"Hive 表 {self.database}.{table_name}",
                        "columns": columns,
                        "primary_key": [],
                        "foreign_keys": [],
                    }
                )
        finally:
            cursor.close()

        return {"tables": tables, "total_tables": len(tables)}

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in rows], columns
        except Exception as exc:
            logger.error(f"Hive query error: {exc}\nSQL: {sql}")
            raise
        finally:
            cursor.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
