"""
达梦数据库数据源适配器

依赖达梦官方 Python 驱动 dmPython。达梦 SQL 与 Oracle 兼容度较高，
MVP 阶段在 SQL 安全解析中使用 oracle 方言。
"""
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from backend.config.config import settings
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
        jdbc_driver_path: str = "",
    ):
        super().__init__(name)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.schema = (schema or user or "").upper()
        self.jdbc_driver_path = jdbc_driver_path
        self._driver = ""
        self._conn = None
        self._metadata_cache: Optional[Dict[str, Any]] = None
        self._metadata_cache_signature = ""
        self._metadata_cache_at = 0.0
        self._metadata_cache_ttl_seconds = float(settings.postgres_metadata_cache_ttl_seconds)

    def get_dialect(self) -> str:
        return "oracle"

    def _get_connection(self):
        if self._conn is None:
            try:
                import dmPython
                self._conn = dmPython.connect(
                    user=self.user,
                    password=self.password,
                    server=self.host,
                    port=self.port,
                )
                self._driver = "dmPython"
                return self._conn
            except ImportError as exc:
                if not self.jdbc_driver_path:
                    raise RuntimeError("达梦数据源需要安装 dmPython，或配置 DAMENG_JDBC_DRIVER_PATH 使用 JDBC 兜底") from exc
                self._conn = self._connect_with_jdbc()
                self._driver = "jdbc"
        return self._conn

    def _connect_with_jdbc(self):
        try:
            import jaydebeapi
        except ImportError as exc:
            raise RuntimeError("达梦 JDBC 兜底连接需要安装 JayDeBeApi 和 JPype1") from exc

        url = f"jdbc:dm://{self.host}:{self.port}/{self.schema}?charset=UTF-8"
        return jaydebeapi.connect(
            "dm.jdbc.driver.DmDriver",
            url,
            [self.user, self.password],
            self.jdbc_driver_path,
        )

    def ping(self) -> None:
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchall()
        finally:
            cursor.close()

    def clear_metadata_cache(self) -> bool:
        self._metadata_cache = None
        self._metadata_cache_signature = ""
        self._metadata_cache_at = 0.0
        return True

    def warmup_metadata_cache(self) -> Dict[str, Any]:
        return self.get_metadata()

    def metadata_cache_status(self) -> Dict[str, Any]:
        now = time.time()
        age_seconds = max(0.0, now - self._metadata_cache_at) if self._metadata_cache else None
        expires_in_seconds = (
            max(0.0, self._metadata_cache_ttl_seconds - age_seconds)
            if age_seconds is not None
            else None
        )
        return {
            "data_source": self.name,
            "supported": True,
            "cached": self._metadata_cache is not None,
            "schema": self.schema,
            "schema_signature": self._metadata_cache_signature,
            "cache_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "ttl_seconds": self._metadata_cache_ttl_seconds,
            "expires_in_seconds": round(expires_in_seconds, 3) if expires_in_seconds is not None else None,
            "total_tables": (self._metadata_cache or {}).get("total_tables"),
        }

    def get_metadata(self) -> Dict[str, Any]:
        conn = self._get_connection()
        now = time.time()
        signature = self._get_schema_signature(conn)
        if (
            self._metadata_cache is not None
            and self._metadata_cache_signature == signature
            and now - self._metadata_cache_at <= self._metadata_cache_ttl_seconds
        ):
            return self._metadata_cache
        cursor = conn.cursor()
        tables: List[Dict[str, Any]] = []
        try:
            cursor.execute(
                """
                SELECT table_name, comments, table_type
                FROM all_tab_comments
                WHERE owner = ? AND table_type IN ('TABLE', 'VIEW')
                ORDER BY table_type, table_name
                """,
                (self.schema,),
            )
            table_rows = cursor.fetchall()
            table_rows = [row for row in table_rows if not str(row[0]).startswith("##")]

            for table_name, table_comment, table_type in table_rows:
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
                if not primary_key:
                    primary_key = self._synthetic_primary_key(table_name)

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
                if not foreign_keys:
                    foreign_keys = self._synthetic_foreign_keys(table_name)

                columns = [
                    {
                        "name": row[0],
                        "type": row[1],
                        "comment": row[3] or self._column_comment(table_name, row[0]),
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
                        "object_type": "view" if table_type == "VIEW" else "table",
                        "columns": columns,
                        "primary_key": primary_key,
                        "foreign_keys": foreign_keys,
                    }
                )
        finally:
            cursor.close()

        metadata = {
            "tables": tables,
            "total_tables": len(tables),
            "schema_signature": signature,
            "schema": self.schema,
        }
        self._metadata_cache = metadata
        self._metadata_cache_signature = signature
        self._metadata_cache_at = now
        return metadata

    def _get_schema_signature(self, conn) -> str:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.nullable,
                    cc.comments
                FROM all_tab_columns c
                LEFT JOIN all_col_comments cc
                  ON cc.owner = c.owner
                 AND cc.table_name = c.table_name
                 AND cc.column_name = c.column_name
                WHERE c.owner = ?
                ORDER BY c.table_name, c.column_id
                """,
                (self.schema,),
            )
            parts = [
                "|".join(str(value or "") for value in row)
                for row in cursor.fetchall()
                if not str(row[0]).startswith("##")
            ]
            return "\n".join(parts)
        finally:
            cursor.close()


    def _synthetic_primary_key(self, table_name: str) -> List[str]:
        if table_name in {"CUSTOMERS", "CATEGORIES", "PRODUCTS", "ORDERS", "ORDER_ITEMS"}:
            return ["ID"]
        return []

    def _synthetic_foreign_keys(self, table_name: str) -> List[Dict[str, str]]:
        relations = {
            "PRODUCTS": [{"column": "CATEGORY_ID", "ref_table": "CATEGORIES", "ref_column": "ID"}],
            "ORDERS": [{"column": "CUSTOMER_ID", "ref_table": "CUSTOMERS", "ref_column": "ID"}],
            "ORDER_ITEMS": [
                {"column": "ORDER_ID", "ref_table": "ORDERS", "ref_column": "ID"},
                {"column": "PRODUCT_ID", "ref_table": "PRODUCTS", "ref_column": "ID"},
            ],
        }
        return relations.get(table_name, [])

    def _column_comment(self, table_name: str, column_name: str) -> str:
        comments = {
            "CUSTOMERS": {"ID": "客户ID", "NAME": "客户姓名", "CITY": "客户所在城市", "AGE": "客户年龄", "REGISTER_DATE": "注册日期", "VIP_LEVEL": "VIP等级，0-5，数字越大等级越高"},
            "CATEGORIES": {"ID": "分类ID", "NAME": "商品分类名称"},
            "PRODUCTS": {"ID": "商品ID", "NAME": "商品名称", "CATEGORY_ID": "所属分类ID，关联 CATEGORIES.ID", "PRICE": "商品单价", "STOCK": "库存数量"},
            "ORDERS": {"ID": "订单ID", "CUSTOMER_ID": "下单客户ID，关联 CUSTOMERS.ID", "ORDER_DATE": "下单日期", "TOTAL_AMOUNT": "订单总金额", "STATUS": "订单状态，pending待支付、paid已支付、shipped已发货、completed已完成、cancelled已取消"},
            "ORDER_ITEMS": {"ID": "订单明细ID", "ORDER_ID": "订单ID，关联 ORDERS.ID", "PRODUCT_ID": "商品ID，关联 PRODUCTS.ID", "QUANTITY": "购买数量", "UNIT_PRICE": "下单时商品单价"},
        }
        return comments.get(table_name, {}).get(column_name, "")

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
