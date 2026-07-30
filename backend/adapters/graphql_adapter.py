"""
GraphQL data source adapter.

The adapter keeps the existing NL2SQL pipeline intact by mapping a GraphQL
response into a read-only in-memory SQLite table, then executing SELECT SQL
against that table.

该适配器通过将 GraphQL 响应映射到只读的内存 SQLite 表，
然后针对该表执行 SELECT SQL，从而保持现有的 NL2SQL 管道完整无损。
"""
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from .base import BaseDataSourceAdapter


DEFAULT_COUNTRIES_QUERY = """
query CountriesForNL2SQL {
  countries {
    code
    name
    native
    phone
    capital
    currency
    emoji
    continent {
      code
      name
    }
    languages {
      code
      name
      native
      rtl
    }
  }
}
""".strip()


class GraphQLAdapter(BaseDataSourceAdapter):
    def __init__(
        self,
        name: str,
        endpoint: str,
        table_name: str = "countries",
        query: str = DEFAULT_COUNTRIES_QUERY,
        data_path: str = "data.countries",
        headers: Optional[Dict[str, str]] = None,
        variables: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
        cache_ttl_seconds: float = 3600.0,
        http_client: Optional[httpx.Client] = None,
    ):
        super().__init__(name)
        if not endpoint:
            raise ValueError("GraphQL 数据源未配置 GRAPHQL_ENDPOINT")
        if not query.strip():
            raise ValueError("GraphQL 数据源未配置 GRAPHQL_QUERY")

        self.endpoint = endpoint
        self.table_name = self._normalize_identifier(table_name or "graphql_records")
        self.query = query
        self.data_path = data_path.strip()
        self.headers = headers or {}
        self.variables = variables or {}
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self.client = http_client or httpx.Client(timeout=timeout)
        self._rows_cache: Optional[List[Dict[str, Any]]] = None
        self._rows_cache_at = 0.0

    def get_dialect(self) -> str:
        return "sqlite"

    def ping(self) -> None:
        self._request_json()

    def get_metadata(self) -> Dict[str, Any]:
        rows = self._fetch_rows()
        columns = self._infer_columns(rows)
        has_code = any(column["name"] == "code" for column in columns)
        return {
            "tables": [
                {
                    "name": self.table_name,
                    "comment": (
                        f"GraphQL 数据源 {self.endpoint} 映射成的只读虚拟表。"
                        "Countries 默认字段包含国家、首都、货币、洲、语言等信息。"
                    ),
                    "columns": columns,
                    "primary_key": ["code"] if has_code else [],
                    "foreign_keys": [],
                },
                {
                    "name": "country_currencies",
                    "comment": "Country to currency relation table split from GraphQL comma-separated currency values.",
                    "columns": [
                        {"name": "country_code", "type": "TEXT", "comment": "Country code", "not_null": True, "default": None, "pk": False},
                        {"name": "currency_code", "type": "TEXT", "comment": "Currency code", "not_null": True, "default": None, "pk": False},
                    ],
                    "primary_key": ["country_code", "currency_code"],
                    "foreign_keys": [{"column": "country_code", "ref_table": self.table_name, "ref_column": "code"}] if has_code else [],
                },
                {
                    "name": "country_languages",
                    "comment": "Country to language relation table split from GraphQL languages array.",
                    "columns": [
                        {"name": "country_code", "type": "TEXT", "comment": "Country code", "not_null": True, "default": None, "pk": False},
                        {"name": "language_code", "type": "TEXT", "comment": "Language code", "not_null": False, "default": None, "pk": False},
                        {"name": "language_name", "type": "TEXT", "comment": "Language name", "not_null": False, "default": None, "pk": False},
                    ],
                    "primary_key": ["country_code", "language_code"],
                    "foreign_keys": [{"column": "country_code", "ref_table": self.table_name, "ref_column": "code"}] if has_code else [],
                },
                {
                    "name": "dict_continent",
                    "comment": "Continent dictionary derived from country continent fields.",
                    "columns": [
                        {"name": "continent_code", "type": "TEXT", "comment": "Continent code", "not_null": True, "default": None, "pk": True},
                        {"name": "continent_name", "type": "TEXT", "comment": "Continent name", "not_null": False, "default": None, "pk": False},
                    ],
                    "primary_key": ["continent_code"],
                    "foreign_keys": [],
                },
                {
                    "name": "v_country_profile",
                    "comment": "Semantic view for country profile with continent, currency codes, and language names.",
                    "columns": [
                        {"name": "country_code", "type": "TEXT", "comment": "Country code", "not_null": False, "default": None, "pk": False},
                        {"name": "country_name", "type": "TEXT", "comment": "Country name", "not_null": False, "default": None, "pk": False},
                        {"name": "native_name", "type": "TEXT", "comment": "Native country name", "not_null": False, "default": None, "pk": False},
                        {"name": "capital", "type": "TEXT", "comment": "Capital", "not_null": False, "default": None, "pk": False},
                        {"name": "calling_code", "type": "TEXT", "comment": "International calling code", "not_null": False, "default": None, "pk": False},
                        {"name": "emoji", "type": "TEXT", "comment": "Country flag emoji", "not_null": False, "default": None, "pk": False},
                        {"name": "continent_code", "type": "TEXT", "comment": "Continent code", "not_null": False, "default": None, "pk": False},
                        {"name": "continent_name", "type": "TEXT", "comment": "Continent name", "not_null": False, "default": None, "pk": False},
                        {"name": "currency_codes", "type": "TEXT", "comment": "Comma-separated currency codes", "not_null": False, "default": None, "pk": False},
                        {"name": "language_codes", "type": "TEXT", "comment": "Comma-separated language codes", "not_null": False, "default": None, "pk": False},
                        {"name": "language_names", "type": "TEXT", "comment": "Comma-separated language names", "not_null": False, "default": None, "pk": False},
                    ],
                    "primary_key": [],
                    "foreign_keys": [],
                },
            ],
            "total_tables": 5,
        }

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        rows = self._fetch_rows()
        columns = self._infer_columns(rows)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            self._load_rows_into_memory_table(conn, rows, columns)
            self._load_country_relation_tables(conn, rows)
            self._create_country_profile_view(conn)
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)

            result_rows = cursor.fetchall()
            result_columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(row) for row in result_rows], result_columns
        except Exception as exc:
            logger.error(f"GraphQL query error: {exc}\nSQL: {sql}")
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
            raise ValueError("GraphQL 响应中的目标数据必须是对象或对象数组")

        rows: List[Dict[str, Any]] = []
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
            response = self.client.post(
                self.endpoint,
                headers=self.headers,
                json={"query": self.query, "variables": self.variables},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("errors"):
                raise RuntimeError(f"GraphQL 返回错误: {payload['errors']}")
            return payload
        except httpx.ConnectTimeout as exc:
            raise RuntimeError(
                f"GraphQL 连接超时：无法在 {self.timeout} 秒内连接 {self.endpoint}"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise RuntimeError(
                f"GraphQL 响应超时：{self.endpoint} 在 {self.timeout} 秒内未返回数据"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"GraphQL 返回 HTTP {exc.response.status_code}：{self.endpoint}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"GraphQL 请求失败：{self.endpoint}，原因：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"GraphQL 返回内容不是合法 JSON：{self.endpoint}") from exc

    def _extract_data(self, payload: Any) -> Any:
        if not self.data_path:
            return payload

        current = payload
        for part in self.data_path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                raise ValueError(f"GraphQL 响应中不存在数据路径: {self.data_path}")
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
                "comment": self._column_comment(name),
                "not_null": False,
                "default": None,
                "pk": name == "code",
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
        column_defs = ", ".join(f'"{column["name"]}" {column["type"]}' for column in columns)
        conn.execute(f'CREATE TABLE "{self.table_name}" ({column_defs})')

        if not rows:
            return

        placeholders = ", ".join("?" for _ in column_names)
        quoted_columns = ", ".join(f'"{name}"' for name in column_names)
        insert_sql = f'INSERT INTO "{self.table_name}" ({quoted_columns}) VALUES ({placeholders})'
        values = []
        for row in rows:
            normalized_to_original = {self._normalize_identifier(key): key for key in row.keys()}
            values.append([
                self._to_sql_value(row.get(normalized_to_original.get(name, name)))
                for name in column_names
            ])
        conn.executemany(insert_sql, values)
        conn.commit()

    def _load_country_relation_tables(self, conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
        conn.execute(
            'CREATE TABLE "country_currencies" ('
            '"country_code" TEXT NOT NULL, '
            '"currency_code" TEXT NOT NULL, '
            'PRIMARY KEY ("country_code", "currency_code"))'
        )
        conn.execute(
            'CREATE TABLE "country_languages" ('
            '"country_code" TEXT NOT NULL, '
            '"language_code" TEXT, '
            '"language_name" TEXT, '
            'PRIMARY KEY ("country_code", "language_code"))'
        )
        conn.execute(
            'CREATE TABLE "dict_continent" ('
            '"continent_code" TEXT PRIMARY KEY, '
            '"continent_name" TEXT)'
        )

        currency_rows = []
        language_rows = []
        continent_rows = {}
        for row in rows:
            country_code = str(row.get("code") or "").strip()
            if not country_code:
                continue
            for currency_code in self._split_csv(row.get("currency")):
                currency_rows.append((country_code, currency_code))

            language_codes = self._split_csv(row.get("language_codes"))
            language_names = self._split_csv(row.get("language_names"))
            for index, language_code in enumerate(language_codes):
                language_name = language_names[index] if index < len(language_names) else ""
                language_rows.append((country_code, language_code, language_name))

            continent_code = str(row.get("continent_code") or "").strip()
            if continent_code:
                continent_rows[continent_code] = str(row.get("continent_name") or "")

        if currency_rows:
            conn.executemany(
                'INSERT OR IGNORE INTO "country_currencies" ("country_code", "currency_code") VALUES (?, ?)',
                currency_rows,
            )
        if language_rows:
            conn.executemany(
                'INSERT OR IGNORE INTO "country_languages" ("country_code", "language_code", "language_name") VALUES (?, ?, ?)',
                language_rows,
            )
        if continent_rows:
            conn.executemany(
                'INSERT OR REPLACE INTO "dict_continent" ("continent_code", "continent_name") VALUES (?, ?)',
                list(continent_rows.items()),
            )
        conn.commit()

    def _create_country_profile_view(self, conn: sqlite3.Connection) -> None:
        conn.execute(f'''
            CREATE VIEW "v_country_profile" AS
            SELECT
                c."code" AS country_code,
                c."name" AS country_name,
                c."native" AS native_name,
                c."capital" AS capital,
                c."phone" AS calling_code,
                c."emoji" AS emoji,
                c."continent_code" AS continent_code,
                c."continent_name" AS continent_name,
                COALESCE((
                    SELECT GROUP_CONCAT(cc."currency_code")
                    FROM "country_currencies" cc
                    WHERE cc."country_code" = c."code"
                ), c."currency") AS currency_codes,
                COALESCE((
                    SELECT GROUP_CONCAT(cl."language_code")
                    FROM "country_languages" cl
                    WHERE cl."country_code" = c."code"
                ), c."language_codes") AS language_codes,
                COALESCE((
                    SELECT GROUP_CONCAT(cl."language_name")
                    FROM "country_languages" cl
                    WHERE cl."country_code" = c."code"
                ), c."language_names") AS language_names
            FROM "{self.table_name}" c
        ''')

    def _split_csv(self, value: Any) -> List[str]:
        if value is None:
            return []
        return [part.strip() for part in str(value).split(",") if part.strip()]

    def _flatten_dict(self, data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
        flat: Dict[str, Any] = {}
        for key, value in data.items():
            normalized_key = self._normalize_identifier(f"{prefix}_{key}" if prefix else str(key))
            if isinstance(value, dict):
                flat.update(self._flatten_dict(value, normalized_key))
            elif isinstance(value, list):
                flat[normalized_key] = json.dumps(value, ensure_ascii=False)
                if normalized_key == "languages":
                    flat["language_codes"] = ", ".join(
                        str(item.get("code", "")) for item in value if isinstance(item, dict)
                    )
                    flat["language_names"] = ", ".join(
                        str(item.get("name", "")) for item in value if isinstance(item, dict)
                    )
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

    def _column_comment(self, name: str) -> str:
        comments = {
            "code": "国家两位代码，例如 CN、US",
            "name": "国家英文名称",
            "native": "国家本地语言名称",
            "phone": "国际电话区号",
            "capital": "首都",
            "currency": "货币代码，多个货币用逗号分隔",
            "emoji": "国家旗帜 emoji",
            "continent_code": "所属洲代码",
            "continent_name": "所属洲名称",
            "languages": "语言原始 JSON 数组",
            "language_codes": "语言代码列表",
            "language_names": "语言名称列表",
        }
        return comments.get(name, "GraphQL 字段")
