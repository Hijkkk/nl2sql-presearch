"""
数据源适配器基类 - 适配器模式核心
所有具体的数据源适配器（SQLite、MySQL、PostgreSQL...）都必须继承这个类
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Tuple, Optional
import sqlglot
from loguru import logger


class BaseDataSourceAdapter(ABC):
    """定义抽象类 所有数据源适配器必须实现此接口"""

    def __init__(self, name: str, config: Dict[str, Any] = None):
        self.name = name  # 适配器名称，如 "sqlite_demo"
        self.config = config or {}  # 连接配置，如 {"db_path": "./data/demo.db"}
        self.dialect = self.get_dialect()  # SQL 方言，如 "sqlite"


    @abstractmethod
    def get_dialect(self) -> str:
        """
        返回 sqlglot 方言名称
        常见值：'sqlite', 'mysql', 'postgres'
        这个方法要求每个子类返回自己的 SQL 方言名称，比如：
        - SQLite 适配器返回 "sqlite"
        - MySQL 适配器返回 "mysql"
        - PostgreSQL 适配器返回 "postgres"
        这个方言值会传给 sqlglot，让它知道按哪种数据库的语法规则来解析 SQL。

        sqlglot 是一个 Python 的 SQL 解析和转换库。它能：
        - 把 SQL 字符串解析成语法树（AST）
        - 在不同数据库方言之间转换 SQL（如 MySQL → PostgreSQL）
        - 验证 SQL 语法是否正确

        它由子类来具体实现并返回值，每个子类返回自己的 SQL 方言名称。
        """
        pass

    @abstractmethod
    def get_metadata(self) -> Dict[str, Any]:
        """
        让 LLM 知道数据库里有哪些表、哪些字段、什么类型。
        LLM 生成 SQL 之前需要"了解"数据库结构，这个方法就是提供这个信息的。
        返回数据库元数据，格式如下：
        {
            "tables": [
                {
                    "name": "employees",
                    "comment": "员工表",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "comment": "主键"},
                        {"name": "name", "type": "VARCHAR", "comment": "姓名"}
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {"column": "department_id", "ref_table": "departments", "ref_column": "id"}
                    ]
                }
            ],
            "total_tables": 3
        }
        """
        pass

    @abstractmethod
    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        """
        执行只读查询
        返回: (results, columns)
        results: List[dict] 行数据  [{"id": 1, "name": "张三"}, {"id": 2, "name": "李四"}]
        columns: List[str] 列名列表  ["id", "name"]
        results（行数据）：给前端展示用，告诉用户查询结果是什么
        columns（列名）：告诉前端表格的表头是什么
        """
        pass

    def clear_metadata_cache(self) -> bool:
        """Clear metadata cache when the adapter supports it."""
        return False

    def warmup_metadata_cache(self) -> Dict[str, Any]:
        """Build metadata cache by reading metadata once."""
        return self.get_metadata()

    def metadata_cache_status(self) -> Dict[str, Any]:
        """Return a minimal cache status for management endpoints."""
        return {
            "data_source": self.name,
            "supported": False,
            "cached": False,
        }

    def validate_only_select(self, sql: str) -> bool:
        """
        【核心安全方法】使用 sqlglot 严格验证是否为只读查询
        这是整个系统安全的第一道防线！
        """
        try:
            # 用 sqlglot 把 SQL 解析成语法树
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
            if parsed is None:
                return False
            # 检查根节点是否是 SELECT 或 WITH（CTE）
            # 递归遍历所有子节点，如果发现 INSERT、UPDATE、DELETE、DROP 等危险节点，直接拒绝

            # 检查根节点是否为 SELECT 或 WITH (CTE)
            if not isinstance(parsed, (sqlglot.exp.Select, sqlglot.exp.With)):
                logger.warning(f"Blocked non-SELECT statement: {type(parsed)}")
                return False

            # 递归检查所有子节点，禁止任何写操作
            for node in parsed.walk():
                if isinstance(node, (
                        sqlglot.exp.Insert, sqlglot.exp.Update, sqlglot.exp.Delete,
                        sqlglot.exp.Drop, sqlglot.exp.Alter, sqlglot.exp.Create,
                        sqlglot.exp.Truncate
                )):
                    logger.warning(f"Blocked dangerous operation in SQL: {type(node)}")
                    return False

            return True

        except Exception as e:
            logger.error(f"SQL validation error: {e}")
            return False

    def get_safe_sql(self, sql: str) -> str:
        """
        安全处理 SQL：
        - 自动添加 LIMIT（防止返回过多数据）
        - 格式化 SQL
            sqlglot 重新输出 SQL 时，会统一格式。
            比如 LLM 可能生成：select   id,name   from   employees where id=1
            格式化后变成：SELECT id, name FROM employees WHERE id = 1
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
            if parsed and not parsed.find(sqlglot.exp.Limit):
                parsed = parsed.limit(1000)
            return parsed.sql(dialect=self.dialect)
        except Exception:
            return sql
