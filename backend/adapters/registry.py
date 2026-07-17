"""
适配器注册表 - 集中管理数据源适配器的创建和缓存
避免 main.py 和 routers 之间的循环导入
"""
from fastapi import HTTPException
from backend.config.config import settings
from .sqlite_adapter import SQLiteAdapter
from .mysql_adapter import MySQLAdapter
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
        else:
            raise HTTPException(
                status_code=404,
                detail=f"数据源 {data_source} 不存在或未配置"
            )
    return ADAPTERS[data_source]

