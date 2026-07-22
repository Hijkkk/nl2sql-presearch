"""
PostgreSQL / 高斯 数据源适配器

高斯常见部署兼容 PostgreSQL 协议，MVP 阶段复用该适配器。
"""
from datetime import date, datetime
from decimal import Decimal
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
        sslmode: str = "",
    ):
        super().__init__(name)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.schema = schema or "public"
        self.sslmode = sslmode
        self._conn = None

    def get_dialect(self) -> str:
        return "postgres"

    def _get_connection(self):
        if self._conn is None or getattr(self._conn, "closed", 1):
            if not self.database:
                raise ValueError(f"{self.name} 未配置数据库名")
            self._conn = self._connect()
            self._conn.autocommit = True
        return self._conn

    def _connect(self):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:
            raise RuntimeError("PostgreSQL/高斯 数据源需要安装 psycopg2-binary") from exc

        connect_kwargs = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "dbname": self.database,
            "cursor_factory": psycopg2.extras.RealDictCursor,
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
        if self.sslmode:
            connect_kwargs["sslmode"] = self.sslmode

        return psycopg2.connect(**connect_kwargs)

    def _reset_connection(self) -> None:
        self.close()
        self._conn = None

    def ping(self) -> None:
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:
            self._reset_connection()
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
                if not primary_key:
                    primary_key = self._synthetic_primary_key(table_name)

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
                if not foreign_keys:
                    foreign_keys = self._synthetic_foreign_keys(table_name)

                columns = [
                    {
                        "name": item["column_name"],
                        "type": item["udt_name"] or item["data_type"],
                        "comment": self._column_comment(table_name, item["column_name"]),
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
        return self._execute_query_once(sql, params, retry=True)

    def _execute_query_once(
        self,
        sql: str,
        params: Optional[Dict] = None,
        retry: bool = False,
    ) -> Tuple[List[Dict], List[str]]:
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                columns = [desc.name for desc in cursor.description] if cursor.description else []
                return [self._normalize_row(dict(row)) for row in rows], columns
        except Exception as exc:
            if retry:
                self._reset_connection()
                return self._execute_query_once(sql, params, retry=False)
            logger.error(f"PostgreSQL/Gauss query error: {exc}\nSQL: {sql}")
            raise

    def _normalize_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {key: self._to_jsonable_value(value) for key, value in row.items()}


    def _synthetic_primary_key(self, table_name: str) -> List[str]:
        if table_name in {"customers", "categories", "products", "orders", "order_items"}:
            return ["id"]
        return []

    def _synthetic_foreign_keys(self, table_name: str) -> List[Dict[str, str]]:
        relations = {
            "products": [{"column": "category_id", "ref_table": "categories", "ref_column": "id"}],
            "orders": [{"column": "customer_id", "ref_table": "customers", "ref_column": "id"}],
            "order_items": [
                {"column": "order_id", "ref_table": "orders", "ref_column": "id"},
                {"column": "product_id", "ref_table": "products", "ref_column": "id"},
            ],
        }
        return relations.get(table_name, [])

    def _column_comment(self, table_name: str, column_name: str) -> str:
        ecommerce_comments = {
            "customers": {
                "id": "客户ID，建议别名为 客户ID",
                "name": "客户姓名，建议别名为 客户姓名",
                "city": "客户所在城市，建议别名为 城市",
                "age": "客户年龄，建议别名为 年龄",
                "register_date": "注册日期，建议别名为 注册日期",
                "vip_level": "VIP等级，0-5，数字越大等级越高，建议别名为 VIP等级",
            },
            "categories": {
                "id": "分类ID，建议别名为 分类ID",
                "name": "商品分类名称，建议别名为 商品分类",
            },
            "products": {
                "id": "商品ID，建议别名为 商品ID",
                "name": "商品名称，建议别名为 商品名称",
                "category_id": "所属分类ID，关联 categories.id",
                "price": "商品单价，建议别名为 单价",
                "stock": "库存数量，建议别名为 库存",
            },
            "orders": {
                "id": "订单ID，建议别名为 订单ID",
                "customer_id": "下单客户ID，关联 customers.id",
                "order_date": "下单日期，建议别名为 下单日期",
                "total_amount": "订单总金额，建议别名为 订单金额",
                "status": "订单状态，pending待支付、paid已支付、shipped已发货、completed已完成、cancelled已取消",
            },
            "order_items": {
                "id": "订单明细ID，建议别名为 明细ID",
                "order_id": "订单ID，关联 orders.id",
                "product_id": "商品ID，关联 products.id",
                "quantity": "购买数量，建议别名为 数量",
                "unit_price": "下单时商品单价，建议别名为 成交单价",
            },
        }
        if table_name in ecommerce_comments:
            return ecommerce_comments.get(table_name, {}).get(column_name, "")

        stock_comments = {
            "stock_daily_prices": {
                "id": "主键ID",
                "symbol": "股票代码，注意不是股票中文名称",
                "exchange_code": "交易所代码，例如 AMEX",
                "trade_date": "交易日期，建议查询结果别名为 交易日期",
                "open_price": "开盘价，建议查询结果别名为 开盘价",
                "high_price": "最高价，建议查询结果别名为 最高价",
                "low_price": "最低价，建议查询结果别名为 最低价",
                "close_price": "收盘价，建议查询结果别名为 收盘价",
                "volume": "成交量，建议查询结果别名为 成交量",
                "created_at": "入库时间",
                "updated_at": "更新时间",
            },
            "stock_daily_prices_staging": {
                "symbol_text": "原始股票代码文本",
                "date_text": "原始交易日期文本",
                "open_text": "原始开盘价文本",
                "high_text": "原始最高价文本",
                "low_text": "原始最低价文本",
                "close_text": "原始收盘价文本",
                "volume_text": "原始成交量文本",
            },
        }
        return stock_comments.get(table_name, {}).get(column_name, "")

    def _to_jsonable_value(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    def close(self) -> None:
        if self._conn is not None and not getattr(self._conn, "closed", 1):
            self._conn.close()
