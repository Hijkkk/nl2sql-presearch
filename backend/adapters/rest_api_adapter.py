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


class RESTAPIAdapter(BaseDataSourceAdapter):
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
        cache_ttl_seconds: float = 60.0,
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

    def get_dialect(self) -> str:
        # REST API 本身没有 SQL 方言；这里返回 sqlite，是因为后端用内存表执行只读 SELECT。
        return "sqlite"

    def ping(self) -> None:
        self._request_json()

    def get_metadata(self) -> Dict[str, Any]:
        rows = self._fetch_rows()
        columns = self._infer_columns(rows)
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

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        rows = self._fetch_rows()
        columns = self._infer_columns(rows)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            self._load_rows_into_memory_table(conn, rows, columns)
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)

            result_rows = cursor.fetchall()
            result_columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(row) for row in result_rows], result_columns
        except Exception as exc:
            logger.error(f"REST API query error: {exc}\nSQL: {sql}")
            raise
        finally:
            conn.close()

    def _fetch_rows(self) -> List[Dict[str, Any]]:
        now = time.time()
        if self._rows_cache is not None and now - self._rows_cache_at <= self.cache_ttl_seconds:
            return self._rows_cache

        payload = self._request_json()
        data = self._extract_data(payload)

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("REST API 返回的数据必须是对象或对象数组")

        rows = []
        for item in data:
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
            [self._to_sql_value(row.get(original_key) if original_key in row else row.get(name)) for name, original_key in self._column_key_pairs(row, column_names)]
            for row in rows
        ]
        conn.executemany(insert_sql, values)
        conn.commit()

    def _column_key_pairs(self, row: Dict[str, Any], column_names: List[str]) -> List[Tuple[str, str]]:
        normalized_to_original = {self._normalize_identifier(key): key for key in row.keys()}
        return [(name, normalized_to_original.get(name, name)) for name in column_names]

    def _flatten_dict(self, data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
        flat: Dict[str, Any] = {}
        for key, value in data.items():
            normalized_key = self._normalize_identifier(f"{prefix}_{key}" if prefix else str(key))
            if isinstance(value, dict):
                flat.update(self._flatten_dict(value, normalized_key))
            elif isinstance(value, list):
                flat[normalized_key] = json.dumps(value, ensure_ascii=False)
            else:
                flat[normalized_key] = value
        return flat

    def _infer_sql_type(self, value: Any) -> str:
        if isinstance(value, bool):
            return "INTEGER"
        if isinstance(value, int):
            return "INTEGER"
        if isinstance(value, float):
            return "REAL"
        return "TEXT"

    def _to_sql_value(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return int(value)
        return value

    def _normalize_identifier(self, value: str) -> str:
        cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in value.strip())
        cleaned = cleaned.strip("_") or "value"
        if cleaned[0].isdigit():
            cleaned = f"col_{cleaned}"
        return cleaned.lower()
