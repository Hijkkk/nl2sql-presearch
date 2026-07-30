"""
PostgreSQL / 高斯 数据源适配器

高斯常见部署兼容 PostgreSQL 协议，MVP 阶段复用该适配器。
"""
from datetime import date, datetime
from decimal import Decimal
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .base import BaseDataSourceAdapter
from backend.config.config import settings


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
        # 4 个缓存字段
        self._metadata_cache: Optional[Dict[str, Any]] = None
        self._metadata_cache_signature = ""
        self._metadata_cache_at = 0.0
        self._metadata_cache_ttl_seconds = float(settings.postgres_metadata_cache_ttl_seconds)

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
        self.clear_metadata_cache()

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
        """
        # 签名相同且 TTL 未过期：复用 metadata。
        # 签名变化：立即重新读取表、字段、外键和视图 metadata。
        # TTL 到期：即使签名相同也会全量刷新一次，以更新注释等非列结构信息
        :return:
        """
        conn = self._get_connection()
        now = time.time()

        # 签名相同且 TTL 未过期：复用 metadata。
        # 签名变化：立即重新读取表、字段、外键和视图 metadata。
        # TTL 到期：即使签名相同也会全量刷新一次，以更新注释等非列结构信息
        signature = self._get_schema_signature(conn)
        if (
                self._metadata_cache is not None
                and self._metadata_cache_signature == signature
                and now - self._metadata_cache_at <= self._metadata_cache_ttl_seconds
        ):
            return self._metadata_cache

        tables: List[Dict[str, Any]] = []

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY table_type, table_name
                """,
                (self.schema,),
            )
            table_rows = cursor.fetchall()

            for row in table_rows:
                table_name = row["table_name"]
                table_type = row.get("table_type") or "BASE TABLE"
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
                        "comment": self._table_comment(table_name),
                        "object_type": "view" if table_type == "VIEW" else "table",
                        "columns": columns,
                        "primary_key": primary_key,
                        "foreign_keys": foreign_keys,
                    }
                )

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
        """
        :param conn:
        :return:
        """
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.udt_name,
                    c.is_nullable,
                    c.column_default
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON t.table_schema = c.table_schema
                 AND t.table_name = c.table_name
                WHERE c.table_schema = %s
                  AND t.table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY c.table_name, c.ordinal_position
                """,
                (self.schema,),
            )
            # 每一行格式化为: table_name|column_name|data_type|nullable|default|key|comment
            parts = [
                "|".join(str(row.get(key) or "") for key in (
                    "table_name",
                    "column_name",
                    "data_type",
                    "udt_name",
                    "is_nullable",
                    "column_default",
                ))
                for row in cursor.fetchall()
            ]
        # users|id|bigint|NO||PRI|
        # users|name|varchar(100)|YES|||
        # users|email|varchar(255)|YES||UNI|
        # sales|id|bigint|NO||PRI|
        # sales|amount|decimal(10,2)|YES|||
        return "\n".join(parts)

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
            "stock_daily_prices": [
                {"column": "symbol", "ref_table": "stock_symbols", "ref_column": "symbol"},
            ],
            "stock_corporate_actions": [
                {"column": "symbol", "ref_table": "stock_symbols", "ref_column": "symbol"},
            ],
            "stock_symbols": [
                {"column": "sic_code", "ref_table": "stock_industry_classification", "ref_column": "sic_code"},
            ],
        }
        return relations.get(table_name, [])

    def _table_comment(self, table_name: str) -> str:
        stock_comments = {
            "stock_daily_prices": (
                "股票日行情事实表，按 symbol + trade_date 存储开盘、最高、最低、收盘、成交量等价格数据。"
                "当用户使用股票中文名、公司名或问“今天/最新收盘价”时，应 JOIN stock_symbols ON stock_daily_prices.symbol = stock_symbols.symbol。"
            ),
            "stock_symbols": (
                "股票证券主数据维表，包含 symbol、security_name、chinese_name、交易所、发行类型、SIC 行业代码等。"
                "中文公司名如“苹果公司”应匹配 chinese_name 或 security_name，再关联行情、ETP 或行业表。"
            ),
            "stock_etp_metadata": (
                "ETF/ETP 扩展属性表，包含 etp_type、leveraged_flag、leveraged_ratio、inverse_flag、underlying_asset。"
                "查询杠杆 ETF/反向 ETF 时应 JOIN stock_symbols 获取名称，并用 leveraged_flag/inverse_flag 判断。"
            ),
            "stock_industry_classification": (
                "SIC 行业分类维表，sic_code 为主键，parent_sic_code 自关联到 sic_code，支持行业树、上级行业、子行业递归 CTE 查询。"
            ),
            "stock_corporate_actions": (
                "股票公司行为表，记录拆股、更名等 action_type、effective_date、old_value/new_value。"
                "应通过 symbol JOIN stock_symbols 获取证券名称。"
            ),
            "stock_daily_prices_staging": "股票行情导入暂存表，仅用于数据清洗，不优先用于业务查询。",
            "stock_daily_prices_backup": "股票行情备份表，仅用于备份核对，不优先用于业务查询。",
        }
        return stock_comments.get(table_name, f"{self.schema}.{table_name}")

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

        stock_core_comments = {
            "stock_daily_prices": {
                "id": "主键ID",
                "symbol": "股票代码，关联 stock_symbols.symbol；如果用户用中文名/公司名提问，应先 JOIN stock_symbols",
                "exchange_code": "交易所代码，例如 NASDAQ、NYSE、AMEX",
                "trade_date": "交易日期；用户说今天/最新时通常应 ORDER BY trade_date DESC LIMIT 1",
                "open_price": "开盘价，建议查询结果别名为 开盘价",
                "high_price": "最高价，建议查询结果别名为 最高价",
                "low_price": "最低价，建议查询结果别名为 最低价",
                "close_price": "收盘价，建议查询结果别名为 收盘价",
                "volume": "成交量，建议查询结果别名为 成交量",
                "volume_weighted_avg_price": "成交量加权平均价 VWAP",
                "trade_count": "成交笔数",
                "previous_close": "前收盘价，可用于计算涨跌额和涨跌幅",
                "created_at": "入库时间",
                "updated_at": "更新时间",
            },
            "stock_corporate_actions": {
                "id": "公司行为记录ID",
                "symbol": "股票代码，关联 stock_symbols.symbol",
                "action_type": "公司行为类型，例如 split、rename、dividend 等",
                "effective_date": "公司行为生效日期",
                "old_value": "变更前取值，例如旧代码或旧名称",
                "new_value": "变更后取值，例如新代码或新名称",
                "description": "公司行为说明",
                "created_at": "入库时间",
            },
            "stock_symbols": {
                "symbol": "股票代码/证券代码，主键，可关联 stock_daily_prices.symbol、stock_etp_metadata.symbol、stock_corporate_actions.symbol",
                "security_name": "证券英文名称/公司英文名称",
                "chinese_name": "证券中文名称/公司中文名，例如 苹果公司；中文提问应优先用此字段匹配",
                "exchange_code": "交易所代码，例如 NASDAQ、NYSE、AMEX",
                "market_category": "市场分类",
                "test_issue": "是否测试证券",
                "financial_status": "财务状态",
                "round_lot_size": "标准交易单位",
                "country_of_incorporation": "注册地国家/地区",
                "ipo_flag": "是否 IPO 相关证券",
                "issue_type": "证券发行类型",
                "sub_issue_type": "证券子类型",
                "sic_code": "SIC 行业代码，关联 stock_industry_classification.sic_code",
                "first_trade_date": "首个交易日期",
                "is_active": "是否仍活跃交易",
                "created_at": "入库时间",
                "updated_at": "更新时间",
            },
            "stock_etp_metadata": {
                "symbol": "ETF/ETP 代码，主键，关联 stock_symbols.symbol",
                "etp_type": "ETP 类型，例如 ETF、ETN 等",
                "leveraged_flag": "是否杠杆 ETP/ETF；查询杠杆 ETF 时应过滤 leveraged_flag = true",
                "leveraged_ratio": "杠杆倍数，例如 2、3",
                "inverse_flag": "是否反向 ETP/ETF",
                "underlying_asset": "跟踪的底层资产或指数",
                "luld_tier": "LULD 分层",
            },
            "stock_industry_classification": {
                "sic_code": "SIC 行业代码，主键，例如 3571",
                "sic_name": "SIC 行业英文名称",
                "chinese_name": "SIC 行业中文名称",
                "division": "SIC 大门类",
                "major_group": "SIC 主组",
                "parent_sic_code": "上级 SIC 代码，自关联 stock_industry_classification.sic_code；树形查询使用递归 CTE",
            },
        }
        if table_name in stock_core_comments:
            return stock_core_comments.get(table_name, {}).get(column_name, "")

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
