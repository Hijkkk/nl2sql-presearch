"""
REST API 数据源适配器

把外部 JSON API 映射为一张只读临时表，复用现有 NL2SQL 查询链路。
注意：这里不会让前端直接访问数据库或外部 API，所有请求都由后端代理完成。
"""
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from .base import BaseDataSourceAdapter


# graph TD
#     A["用户提问（自然语言）"] --> B["大模型生成 SQL"]
#     B --> C["REST API 适配器执行 SQL"]
#     C --> D["调用第三方 API 获取 JSON 数据"]
#     D --> E["数据整理/展平"]
#     E --> F["加载到内存 SQLite 临时表"]
#     F --> G["在内存 SQLite 上执行 SQL 查询"]
#     G --> H["返回查询结果"]


# 调用第三方 API 获取 JSON 数据（_fetch_rows 方法，第111行）
# 通过 httpx 发起 HTTP GET 请求
# 按配置的 data_path（如 "data.list"）从 JSON 中提取目标数据
# 嵌套的 dict 会被展平（_flatten_dict），list 会被序列化为 JSON 字符串
# 加载到内存 SQLite 表（_load_rows_into_memory_table 方法，第219行）
# sqlite3.connect(":memory:") 创建一个纯内存的临时 SQLite 数据库
# 根据 JSON 的 key 自动推断列名和类型（_infer_columns）
# CREATE TABLE 建表，然后 INSERT 所有数据
# 在内存表上执行大模型生成的 SQL（execute_query 方法，第88行）
# 大模型生成的 SQL 语句在这个内存 SQLite 上执行
# 返回查询结果

# ：把第三方 API 的 JSON 数据"伪装"成一张 SQLite 表，
# 这样就能复用已有的 NL2SQL 链路（大模型生成 SQL → 执行 SQL → 返回结果），
# 而不需要为 REST API 单独实现一套查询逻辑。这是一个很巧妙的设计。

# 第 1 步：调用 API，拿到 JSON
# {
#   "status": "1",
#   "count": "1",
#   "info": "OK",
#   "lives": [
#     {
#       "province": "北京",
#       "city": "北京市",
#       "adcode": "110000",
#       "weather": "多云",
#       "temperature": "28",
#       "winddirection": "南",
#       "windpower": "≤3",
#       "humidity": "55",
#       "reporttime": "2026-07-21 14:00"
#     }
#   ]
# }
# 第 2 步：_extract_data 按 data_path=lives 提取
# # data_path = "lives"，从 JSON 中取出 lives 字段
# data = [
#     {
#       "province": "北京",
#       "city": "北京市",
#       "adcode": "110000",
#       "weather": "多云",
#       "temperature": "28",
#       "winddirection": "南",
#       "windpower": "≤3",
#       "humidity": "55",
#       "reporttime": "2026-07-21 14:00"
#     }
# ]
class RESTAPIAdapter(BaseDataSourceAdapter):
    # name
    # 数据源名称，如 "rest_api_demo"
    # url
    # API 地址，如 "https://jsonplaceholder.typicode.com/posts"
    # table_name
    # 映射成的虚拟表名，如 "posts"
    # data_path
    # JSON 数据提取路径，如 "data.list"
    # headers
    # 请求头，如 {"Authorization": "Bearer xxx"}
    # timeout
    # 请求超时时间（秒）
    # http_client
    # HTTP 客户端（可选，用于测试注入）
    def __init__(
            self,
            name: str,
            url: str,
            table_name: str = "api_records",
            data_path: str = "",
            headers: Optional[Dict[str, str]] = None,
            query_params: Optional[Dict[str, Any]] = None,
            api_key_param: str = "",
            api_key: str = "",
            timeout: float = 10.0,
            cache_ttl_seconds: float = 60.0,  # API 返回的数据会缓存 cache_ttl_seconds（默认60秒），避免每次查询都重新请求第三方 API。
            http_client: Optional[httpx.Client] = None,
    ):
        super().__init__(name)
        if not url:
            raise ValueError("REST API 数据源未配置 REST_API_URL")

        self.url = url
        self.table_name = self._normalize_identifier(table_name or "api_records")
        self.data_path = data_path.strip()
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.api_key_param = api_key_param.strip()
        self.api_key = api_key
        self.timeout = timeout
        self.client = http_client or httpx.Client(timeout=timeout)
        self._rows_cache: Optional[List[Dict[str, Any]]] = None
        self._rows_cache_at = 0.0
        self.cache_ttl_seconds = cache_ttl_seconds

    # 返回 "sqlite"，因为 REST API 数据会被加载到内存 SQLite 表中执行查询。
    def get_dialect(self) -> str:
        # REST API 本身没有 SQL 方言；这里返回 sqlite，是因为后端用内存表执行只读 SELECT。
        return "sqlite"

    # 发一次请求测试 API 是否可用。成功就返回，失败就抛异常。
    def ping(self) -> None:
        self._request_json()

    # 返回 REST API 数据的"表结构"，让 NL2SQL 系统知道有哪些字段。
    def get_metadata(self) -> Dict[str, Any]:
        rows = self._fetch_rows()  # 1. 请求 API 获取数据
        columns = self._infer_columns(rows)  # 2. 根据数据推断字段类型
        # # 返回格式：
        # {
        #     "tables": [{
        #         "name": "posts",
        #         "comment": "由 REST API https://... 返回的 JSON 数据映射而来",
        #         "columns": [
        #             {"name": "id", "type": "INTEGER", "comment": "REST API 字段", ...},
        #             {"name": "title", "type": "TEXT", "comment": "REST API 字段", ...},
        #             {"name": "body", "type": "TEXT", "comment": "REST API 字段", ...},
        #         ],
        #         "primary_key": [],
        #         "foreign_keys": [],
        #     }],
        #     "total_tables": 1,
        # }
        return {
            "tables": [
                {
                    "name": self.table_name,
                    "comment": f"由 REST API {self.url} 返回的 JSON 数据映射而来",
                    "columns": columns,
                    "primary_key": [],
                    "foreign_keys": [],
                }
            ],
            "total_tables": 1,
        }

    # 把 REST API 数据加载到内存 SQLite 表，执行 SQL，返回结果。
    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        rows = self._fetch_rows() # 1. 请求 API 获取数据
        columns = self._infer_columns(rows) # 2. 根据数据推断字段类型

        conn = sqlite3.connect(":memory:") # 3. 创建内存数据库
        conn.row_factory = sqlite3.Row
        try:
            self._load_rows_into_memory_table(conn, rows, columns)  # 4. 建表 + 插入数据
            cursor = conn.cursor()  # 5. 创建游标对象
            if params:
                cursor.execute(sql, params)  # 6. 执行 SQL 查询
            else:
                cursor.execute(sql)  # 6. 执行 SQL 查询

            # # → result_columns = ["city", "weather", "temperature", "humidity"]
            # # → result_rows = [("北京市", "多云", "28", "55")]
            result_rows = cursor.fetchall()  # 7. 获取查询结果
            result_columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(row) for row in result_rows], result_columns
        except Exception as exc:
            logger.error(f"REST API query error: {exc}\nSQL: {sql}")
            raise
        finally:
            conn.close()

    # 请求 API 获取数据，60 秒内复用缓存，避免频繁请求。
    def _fetch_rows(self) -> List[Dict[str, Any]]:
        now = time.time()
        # 缓存未过期 → 直接返回
        if self._rows_cache is not None and now - self._rows_cache_at <= self.cache_ttl_seconds:
            return self._rows_cache
        # 缓存过期或首次 → 重新请求
        payload = self._request_json()  # 1. 发 HTTP 请求
        data = self._extract_data(payload)  # 2. 提取目标数据（处理嵌套）
        # 统一转成列表
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("REST API 返回的数据必须是对象或对象数组")

        rows = []
        for item in data:
            # 扁平化每条记录（嵌套字典展开）
            if isinstance(item, dict):
                rows.append(self._flatten_dict(item))
            else:
                rows.append({"value": item})
        self._rows_cache = rows
        self._rows_cache_at = now
        return rows

    def _request_json(self) -> Any:
        try:
            response = self.client.get(
                self.url,
                headers=self.headers,
                params=self._build_query_params(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            self._validate_business_status(payload)
            return payload
        except httpx.ConnectTimeout as exc:
            raise RuntimeError(
                f"REST API 连接超时：无法在 {self.timeout} 秒内连接 {self.url}，"
                "请检查外网访问、代理、DNS 或更换可访问的 API 地址"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise RuntimeError(
                f"REST API 响应超时：{self.url} 在 {self.timeout} 秒内未返回数据"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"REST API 返回 HTTP {exc.response.status_code}：{self.url}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"REST API 请求失败：{self.url}，原因：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"REST API 返回内容不是合法 JSON：{self.url}") from exc

    def _build_query_params(self) -> Dict[str, Any]:
        params = dict(self.query_params)
        if self.api_key_param and self.api_key:
            params[self.api_key_param] = self.api_key
        return params

    def _validate_business_status(self, payload: Any) -> None:
        """兼容高德等 API 的业务状态码：HTTP 200 但 status=0 表示业务失败。"""
        if not isinstance(payload, dict):
            return

        status = payload.get("status")
        if status not in (0, "0"):
            return

        info = payload.get("info") or payload.get("message") or "未知错误"
        infocode = payload.get("infocode")
        if infocode:
            raise RuntimeError(f"REST API 业务错误：{info}，infocode={infocode}")
        raise RuntimeError(f"REST API 业务错误：{info}")

    # # data_path = "lives"，从 JSON 中取出 lives 字段
    # data = [
    #     {
    #       "province": "北京",
    #       "city": "北京市",
    #       "adcode": "110000",
    #       "weather": "多云",
    #       "temperature": "28",
    #       "winddirection": "南",
    #       "windpower": "≤3",
    #       "humidity": "55",
    #       "reporttime": "2026-07-21 14:00"
    #     }
    # ]
    def _extract_data(self, payload: Any) -> Any:
        if not self.data_path:
            return payload

        current = payload
        for part in self.data_path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                raise ValueError(f"REST API 响应中不存在数据路径: {self.data_path}")
        return current

    # 遍历数据的每个 key，根据 value 的 Python 类型推断 SQL 类型
    def _infer_columns(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        column_types: Dict[str, str] = {}
        for row in rows:
            for key, value in row.items():
                column_name = self._normalize_identifier(key)
                column_types[column_name] = self._infer_sql_type(value)

        if not column_types:
            column_types["value"] = "TEXT"

        return [
            {
                "name": name,
                "type": column_type,
                "comment": "REST API 字段",
                "not_null": False,
                "default": None,
                "pk": False,
            }
            for name, column_type in column_types.items()
        ]

    # ：_load_rows_into_memory_table 建内存表
    # -- 在 :memory: 中执行
    # CREATE TABLE "amap_weather" (
    #     "province" TEXT,
    #     "city" TEXT,
    #     "adcode" TEXT,
    #     "weather" TEXT,
    #     "temperature" TEXT,
    #     "winddirection" TEXT,
    #     "windpower" TEXT,
    #     "humidity" TEXT,
    #     "reporttime" TEXT
    # )
    #
    # INSERT INTO "amap_weather" VALUES ('北京', '北京市', '110000', '多云', '28', '南', '≤3', '55', '2026-07-21 14:00')
    def _load_rows_into_memory_table(
            self,
            conn: sqlite3.Connection,
            rows: List[Dict[str, Any]],
            columns: List[Dict[str, Any]],
    ) -> None:
        column_names = [column["name"] for column in columns]
        column_defs = ", ".join(f'"{name}" {column["type"]}' for name, column in zip(column_names, columns))
        conn.execute(f'CREATE TABLE "{self.table_name}" ({column_defs})')

        if not rows:
            return

        placeholders = ", ".join("?" for _ in column_names)
        quoted_columns = ", ".join(f'"{name}"' for name in column_names)
        insert_sql = f'INSERT INTO "{self.table_name}" ({quoted_columns}) VALUES ({placeholders})'
        values = [
            [self._to_sql_value(row.get(original_key) if original_key in row else row.get(name)) for name, original_key
             in self._column_key_pairs(row, column_names)]
            for row in rows
        ]
        conn.executemany(insert_sql, values)
        conn.commit()

    def _column_key_pairs(self, row: Dict[str, Any], column_names: List[str]) -> List[Tuple[str, str]]:
        normalized_to_original = {self._normalize_identifier(key): key for key in row.keys()}
        return [(name, normalized_to_original.get(name, name)) for name in column_names]

    # 把嵌套的 JSON 对象展开成一层，用下划线连接键名。
    # # 输入：
    # {
    #     "user": {"name": "张三", "age": 25},
    #     "order": {"id": 100, "items": [1, 2, 3]},
    #     "status": "paid"
    # }
    #
    # # 输出：
    # {
    #     "user_name": "张三",
    #     "user_age": 25,
    #     "order_id": 100,
    #     "order_items": "[1, 2, 3]",    # 列表转 JSON 字符串
    #     "status": "paid"
    # }
    def _flatten_dict(self, data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
        flat: Dict[str, Any] = {}
        for key, value in data.items():
            # 格式化
            normalized_key = self._normalize_identifier(f"{prefix}_{key}" if prefix else str(key))
            if isinstance(value, dict):
                flat.update(self._flatten_dict(value, normalized_key))
            elif isinstance(value, list):
                flat[normalized_key] = json.dumps(value, ensure_ascii=False)
            else:
                flat[normalized_key] = value
        return flat

    # 根据 Python 值返回对应的 SQL 类型名。
    def _infer_sql_type(self, value: Any) -> str:
        if isinstance(value, bool):
            return "INTEGER"
        if isinstance(value, int):
            return "INTEGER"
        if isinstance(value, float):
            return "REAL"
        return "TEXT"

    # 把 Python 值转成 SQLite 能存储的格式。
    def _to_sql_value(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return int(value)
        return value

    # 把任意字符串转成合法的 SQL 字段名（小写、字母数字下划线）。
    # _normalize_identifier("User-ID")     → "user_id"
    # _normalize_identifier("first name")  → "first_name"
    # _normalize_identifier("2nd_column")  → "col_2nd_column"   # 数字开头加前缀
    # _normalize_identifier("hello@world") → "hello_world"       # 特殊字符转下划线
    def _normalize_identifier(self, value: str) -> str:
        cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in value.strip())
        cleaned = cleaned.strip("_") or "value"
        if cleaned[0].isdigit():
            cleaned = f"col_{cleaned}"
        return cleaned.lower()
