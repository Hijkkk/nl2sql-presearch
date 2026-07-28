"""
适配器注册表 - 集中管理数据源适配器的创建和缓存
避免 main.py 和 routers 之间的循环导入
"""
import json
import os

from fastapi import HTTPException
from backend.config.config import settings
from .sqlite_adapter import SQLiteAdapter
from .mysql_adapter import MySQLAdapter
from .postgres_adapter import PostgreSQLAdapter
from .hive_adapter import HadoopLocalDemoAdapter, HiveAdapter
from .dameng_adapter import DamengAdapter
from .rest_api_adapter import RESTAPIAdapter
from .graphql_adapter import DEFAULT_COUNTRIES_QUERY, GraphQLAdapter
from .base import BaseDataSourceAdapter

# 全局适配器注册表
"""
ADAPTERS = {
    "sqlite_demo": <SQLiteAdapter 对象>,
    "mysql_prod": <MySQLAdapter 对象>,      # 未来扩展
    "postgres_analytics": <PostgresAdapter 对象>,  # 未来扩展
}
用字典缓存，只创建一次
"""
ADAPTERS: dict[str, BaseDataSourceAdapter] = {}

"""
加载源数据--->可扩展
    # 前端请求：GET /api/v1/metadata/sqlite_demo
    adapter = get_adapter("sqlite_demo")

    1. 检查 "sqlite_demo" 在 ADAPTERS 里吗？
       → 不在（ADAPTERS 是空的 {}）
    2. 创建 SQLiteAdapter 对象
       ADAPTERS["sqlite_demo"] = SQLiteAdapter(...)
    3. 返回 ADAPTERS["sqlite_demo"]
"""
def get_adapter(data_source: str) -> BaseDataSourceAdapter:
    if data_source not in ADAPTERS:
        if data_source == "sqlite_demo":
            ADAPTERS[data_source] = SQLiteAdapter(
                name="sqlite_demo",
                db_path=settings.sqlite_db_path
            )
        elif data_source == settings.mysql_query_name:
            if not settings.mysql_query_enabled:
                raise HTTPException(
                    status_code=404,
                    detail=f"MySQL 数据源 {data_source} 未启用"
                )
            if not settings.mysql_query_database:
                raise HTTPException(
                    status_code=404,
                    detail="MySQL 查询数据源未配置 MYSQL_QUERY_DATABASE"
                )
            ADAPTERS[data_source] = MySQLAdapter(
                name=settings.mysql_query_name,
                host=settings.mysql_query_host,
                port=settings.mysql_query_port,
                user=settings.mysql_query_user,
                password=settings.mysql_query_password,
                database=settings.mysql_query_database,
                charset=settings.mysql_query_charset,
            )
        elif data_source == settings.postgres_query_name:
            if not settings.postgres_query_enabled:
                raise HTTPException(
                    status_code=404,
                    detail=f"PostgreSQL 数据源 {data_source} 未启用"
                )
            ADAPTERS[data_source] = PostgreSQLAdapter(
                name=settings.postgres_query_name,
                host=settings.postgres_query_host,
                port=settings.postgres_query_port,
                user=settings.postgres_query_user,
                password=settings.postgres_query_password,
                database=settings.postgres_query_database,
                schema=settings.postgres_query_schema,
                sslmode=settings.postgres_query_sslmode,
            )
        elif data_source == settings.gauss_query_name:
            if not settings.gauss_query_enabled:
                raise HTTPException(
                    status_code=404,
                    detail=f"高斯数据源 {data_source} 未启用"
                )
            ADAPTERS[data_source] = PostgreSQLAdapter(
                name=settings.gauss_query_name,
                host=settings.gauss_query_host,
                port=settings.gauss_query_port,
                user=settings.gauss_query_user,
                password=settings.gauss_query_password,
                database=settings.gauss_query_database,
                schema=settings.gauss_query_schema,
                sslmode="",
            )
        elif data_source == settings.hive_query_name:
            if not settings.hive_query_enabled:
                raise HTTPException(
                    status_code=404,
                    detail=f"Hive/Hadoop 数据源 {data_source} 未启用"
                )
            if settings.hive_query_mode == "local_demo":
                # 优先用 data_dir（新方案：扫描目录下所有 hadoop_*.csv 多表星型模型）
                # 兼容老配置 hive_demo_csv_path（单文件）
                data_dir = settings.hive_demo_data_dir or os.path.dirname(settings.hive_demo_csv_path)
                ADAPTERS[data_source] = HadoopLocalDemoAdapter(
                    name=settings.hive_query_name,
                    data_dir=data_dir,
                )
            else:
                ADAPTERS[data_source] = HiveAdapter(
                    name=settings.hive_query_name,
                    host=settings.hive_query_host,
                    port=settings.hive_query_port,
                    username=settings.hive_query_user,
                    password=settings.hive_query_password,
                    database=settings.hive_query_database,
                    auth=settings.hive_query_auth,
                )
        elif data_source == settings.dameng_query_name:
            if not settings.dameng_query_enabled:
                raise HTTPException(
                    status_code=404,
                    detail=f"达梦数据源 {data_source} 未启用"
                )
            ADAPTERS[data_source] = DamengAdapter(
                name=settings.dameng_query_name,
                host=settings.dameng_query_host,
                port=settings.dameng_query_port,
                user=settings.dameng_query_user,
                password=settings.dameng_query_password,
                schema=settings.dameng_query_schema,
                jdbc_driver_path=settings.dameng_jdbc_driver_path,
            )
        elif data_source == settings.rest_api_name:
            if not settings.rest_api_enabled:
                raise HTTPException(
                    status_code=404,
                    detail=f"REST API 数据源 {data_source} 未启用"
                )
            try:
                headers = json.loads(settings.rest_api_headers_json or "{}")
                if not isinstance(headers, dict):
                    raise ValueError("REST_API_HEADERS_JSON 必须是 JSON 对象")
                query_params = json.loads(settings.rest_api_query_params_json or "{}")
                if not isinstance(query_params, dict):
                    raise ValueError("REST_API_QUERY_PARAMS_JSON 必须是 JSON 对象")
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"REST API 配置错误: {exc}"
                )

            ADAPTERS[data_source] = RESTAPIAdapter(
                name=settings.rest_api_name,
                url=settings.rest_api_url,
                table_name=settings.rest_api_table_name,
                data_path=settings.rest_api_data_path,
                headers={str(key): str(value) for key, value in headers.items()},
                query_params=query_params,
                api_key_param=settings.rest_api_key_param,
                api_key=settings.rest_api_api_key,
                timeout=settings.rest_api_timeout,
                cache_ttl_seconds=settings.rest_api_cache_ttl_seconds,
            )
        elif data_source == settings.graphql_name:
            if not settings.graphql_enabled:
                raise HTTPException(
                    status_code=404,
                    detail=f"GraphQL 数据源 {data_source} 未启用"
                )
            try:
                headers = json.loads(settings.graphql_headers_json or "{}")
                if not isinstance(headers, dict):
                    raise ValueError("GRAPHQL_HEADERS_JSON 必须是 JSON 对象")
                variables = json.loads(settings.graphql_variables_json or "{}")
                if not isinstance(variables, dict):
                    raise ValueError("GRAPHQL_VARIABLES_JSON 必须是 JSON 对象")
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"GraphQL 配置错误: {exc}"
                )

            ADAPTERS[data_source] = GraphQLAdapter(
                name=settings.graphql_name,
                endpoint=settings.graphql_endpoint,
                table_name=settings.graphql_table_name,
                query=settings.graphql_query or DEFAULT_COUNTRIES_QUERY,
                data_path=settings.graphql_data_path,
                headers={str(key): str(value) for key, value in headers.items()},
                variables=variables,
                timeout=settings.graphql_timeout,
                cache_ttl_seconds=settings.graphql_cache_ttl_seconds,
            )
        else:
            raise HTTPException(
                status_code=404,
                detail=f"数据源 {data_source} 不存在或未配置"
            )
    return ADAPTERS[data_source]

