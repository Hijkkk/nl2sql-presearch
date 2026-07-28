"""
Hive / Hadoop 数据源适配器

Hadoop 是存储和计算生态，NL2SQL 通常通过 HiveServer2 暴露 SQL 能力。
"""
import csv
import os
import sqlite3
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


class HadoopLocalDemoAdapter(BaseDataSourceAdapter):
    """
    本地 Hadoop/Hive 演示适配器（多 CSV 多表星型模型）。

    真实生产链路应使用 HiveAdapter 连接 HiveServer2。课题演示环境没有 HiveServer2 时，
    用本地 data_dir 下的多个 CSV 模拟 HDFS 上的事实表 + 维度表，并通过内存 SQLite
    暴露多表 SQL 查询能力。

    目录结构：
        backend/data/hadoop/hadoop_order_events.csv   # 订单事实
        backend/data/hadoop/hadoop_user_profiles.csv  # 用户维度
        backend/data/hadoop/hadoop_product_dim.csv    # 商品维度
        backend/data/hadoop/hadoop_region_dim.csv     # 地域维度
    """

    # 合成的元数据（表注释 / 列注释 / PK / FK），prompt 友好，便于 LLM 自动 JOIN
    _SCHEMA: Dict[str, Dict[str, Any]] = {
        "hadoop_order_events": {
            "comment": "HDFS 上的订单事件事实表（30 行 2024 年订单），含订单 ID / 日期 / 用户 / 商品 / 地域 / 数量 / 金额。问订单问题时，模型应 JOIN user / product / region 三张维度表。",
            "columns": [
                {"name": "event_id",     "type": "TEXT",    "comment": "订单事件 ID，主键 E0001-E0030", "pk": True},
                {"name": "event_date",   "type": "TEXT",    "comment": "事件日期，YYYY-MM-DD", "pk": False},
                {"name": "user_id",      "type": "INTEGER", "comment": "下单用户 ID，关联 hadoop_user_profiles.user_id", "pk": False},
                {"name": "product_id",   "type": "TEXT",    "comment": "商品 ID，关联 hadoop_product_dim.product_id", "pk": False},
                {"name": "region_id",    "type": "TEXT",    "comment": "地域 ID，关联 hadoop_region_dim.region_id", "pk": False},
                {"name": "order_count",  "type": "INTEGER", "comment": "订单数量，建议别名为 订单数", "pk": False},
                {"name": "gmv",          "type": "REAL",    "comment": "成交金额（人民币元），建议别名为 成交金额", "pk": False},
            ],
            "primary_key": ["event_id"],
            "foreign_keys": [
                {"column": "user_id",    "ref_table": "hadoop_user_profiles", "ref_column": "user_id"},
                {"column": "product_id", "ref_table": "hadoop_product_dim",   "ref_column": "product_id"},
                {"column": "region_id",  "ref_table": "hadoop_region_dim",    "ref_column": "region_id"},
            ],
        },
        "hadoop_user_profiles": {
            "comment": "HDFS 上的用户维度表（15 个用户），含用户 ID / 姓名 / 年龄 / 性别 / VIP 等级 / 注册日 / 所在地域。问用户画像或会员情况时使用，可 JOIN hadoop_region_dim 取省市区。",
            "columns": [
                {"name": "user_id",       "type": "INTEGER", "comment": "用户 ID，主键 1001-1015", "pk": True},
                {"name": "user_name",     "type": "TEXT",    "comment": "用户姓名", "pk": False},
                {"name": "age",           "type": "INTEGER", "comment": "年龄", "pk": False},
                {"name": "gender",        "type": "TEXT",    "comment": "性别：M / F", "pk": False},
                {"name": "vip_level",     "type": "INTEGER", "comment": "VIP 等级，2-5，5 为最高", "pk": False},
                {"name": "register_date", "type": "TEXT",    "comment": "注册日期，YYYY-MM-DD", "pk": False},
                {"name": "region_id",     "type": "TEXT",    "comment": "用户所在地域 ID，关联 hadoop_region_dim.region_id", "pk": False},
            ],
            "primary_key": ["user_id"],
            "foreign_keys": [
                {"column": "region_id", "ref_table": "hadoop_region_dim", "ref_column": "region_id"},
            ],
        },
        "hadoop_product_dim": {
            "comment": "HDFS 上的商品维度表（15 个 SKU），含商品 ID / 名称 / 一级分类 / 二级分类 / 品牌。问具体商品或品类销量时使用。",
            "columns": [
                {"name": "product_id",   "type": "TEXT",    "comment": "商品 ID，主键 P001-P015", "pk": True},
                {"name": "product_name", "type": "TEXT",    "comment": "商品名称", "pk": False},
                {"name": "category",     "type": "TEXT",    "comment": "一级分类：手机数码 / 电脑办公 / 家用电器 / 服饰鞋包 / 美妆个护", "pk": False},
                {"name": "sub_category", "type": "TEXT",    "comment": "二级分类：智能手机 / 笔记本电脑 / 空调 / 护肤 等", "pk": False},
                {"name": "brand",        "type": "TEXT",    "comment": "品牌：Apple / Xiaomi / Huawei / Nike 等", "pk": False},
            ],
            "primary_key": ["product_id"],
            "foreign_keys": [],
        },
        "hadoop_region_dim": {
            "comment": "HDFS 上的地域维度表（12 个城市），含地域 ID / 省 / 市 / 城市分级 / 大区。问地区销售或城市层级时使用。",
            "columns": [
                {"name": "region_id",    "type": "TEXT", "comment": "地域 ID，主键 R001-R012", "pk": True},
                {"name": "province",     "type": "TEXT", "comment": "省份", "pk": False},
                {"name": "city",         "type": "TEXT", "comment": "城市", "pk": False},
                {"name": "city_tier",    "type": "TEXT", "comment": "城市分级：一线 / 新一线 / 二线 / 三线", "pk": False},
                {"name": "region_group", "type": "TEXT", "comment": "大区：华东 / 华南 / 华北 / 华中 / 西南 / 西北", "pk": False},
            ],
            "primary_key": ["region_id"],
            "foreign_keys": [],
        },
    }

    # SQLite CREATE TABLE 时 INTEGER/REAL/TEXT 三种类型够用
    _COL_TYPE_MAP = {
        "INTEGER": "INTEGER", "INT": "INTEGER",
        "REAL":    "REAL",    "FLOAT": "REAL", "DOUBLE": "REAL", "NUMERIC": "REAL",
        "TEXT":    "TEXT",    "VARCHAR": "TEXT", "STRING": "TEXT", "DATE": "TEXT",
    }

    def __init__(self, name: str, data_dir: str):
        super().__init__(name)
        self.data_dir = data_dir

    def get_dialect(self) -> str:
        return "sqlite"

    def ping(self) -> None:
        self._read_all_tables()

    def get_metadata(self) -> Dict[str, Any]:
        table_names = self._discover_tables()
        tables_meta = []
        for tbl in table_names:
            schema = self._SCHEMA.get(tbl)
            if schema is None:
                rows = self._read_rows(tbl)
                tables_meta.append({
                    "name": tbl,
                    "comment": "未声明的扩展表（hadoop 目录下发现但未在适配器中定义元数据）",
                    "columns": [
                        {"name": k, "type": "TEXT", "comment": "", "not_null": False, "default": None, "pk": False}
                        for k in (rows[0].keys() if rows else [])
                    ],
                    "primary_key": [],
                    "foreign_keys": [],
                })
            else:
                tables_meta.append({
                    "name": tbl,
                    "comment": schema["comment"],
                    "columns": [
                        {**c, "not_null": c.get("pk", False), "default": None}
                        for c in schema["columns"]
                    ],
                    "primary_key": schema["primary_key"],
                    "foreign_keys": schema["foreign_keys"],
                })
        return {"tables": tables_meta, "total_tables": len(tables_meta)}

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        tables = self._discover_tables()
        all_rows: Dict[str, List[Dict[str, str]]] = {t: self._read_rows(t) for t in tables}
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            for tbl in tables:
                rows = all_rows[tbl]
                if not rows:
                    continue
                schema = self._SCHEMA.get(tbl, {})
                col_defs = schema.get("columns") or [{"name": k, "type": "TEXT"} for k in rows[0].keys()]
                col_defs_sql = ", ".join(
                    f'"{c["name"]}" {self._COL_TYPE_MAP.get(c["type"].upper(), "TEXT")}' for c in col_defs
                )
                conn.execute(f'CREATE TABLE "{tbl}" ({col_defs_sql})')
                col_names = [c["name"] for c in col_defs]
                placeholders = ", ".join(["?"] * len(col_names))
                quoted_cols = ", ".join('"' + n + '"' for n in col_names)
                conn.executemany(
                    f'INSERT INTO "{tbl}" ({quoted_cols}) VALUES ({placeholders})',
                    [_coerce_row(r, col_defs) for r in rows],
                )
            cursor = conn.cursor()
            cursor.execute(sql, params or {})
            result_rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(row) for row in result_rows], columns
        finally:
            conn.close()

    # ---------------- 内部工具 ----------------

    def _read_all_tables(self) -> None:
        """触发一次所有表的读取，用于 ping 校验。"""
        for t in self._discover_tables():
            self._read_rows(t)

    def _discover_tables(self) -> List[str]:
        """扫描 data_dir 下所有 hadoop_*.csv，按文件名（去后缀）作为表名。"""
        if not os.path.isdir(self.data_dir):
            return []
        names: List[str] = []
        for fname in sorted(os.listdir(self.data_dir)):
            if fname.lower().endswith(".csv") and fname.lower().startswith("hadoop_"):
                names.append(os.path.splitext(fname)[0])
        return names

    def _read_rows(self, table_name: str) -> List[Dict[str, str]]:
        path = os.path.join(self.data_dir, f"{table_name}.csv")
        if not os.path.isfile(path):
            return []
        with open(path, newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))


def _coerce(value: Any, col_type: str) -> Any:
    """单值类型转换。"""
    if value is None or value == "":
        return None
    t = col_type.upper()
    try:
        if t in ("INTEGER", "INT"):
            return int(value)
        if t in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
            return float(value)
    except (ValueError, TypeError):
        return value
    return value


def _coerce_row(row: Dict[str, str], col_defs: List[Dict[str, Any]]) -> Tuple[Any, ...]:
    """按列定义把一行 dict 转换为元组，类型不匹配保留原字符串。"""
    return tuple(_coerce(row.get(c["name"]), c["type"]) for c in col_defs)
