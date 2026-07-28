"""
SQL 安全守卫 - 使用 sqlglot 进行严格的只读验证
这是整个 NL2SQL 系统最核心的安全组件
系统的安全核心
    使用 sqlglot 对 LLM 生成的 SQL 进行严格的 AST 解析
    确保只能执行 SELECT / WITH（CTE），禁止任何写操作
    提供 sanitize_for_execution 方法，自动给 SQL 加 LIMIT 保护
"""
import sqlglot
import re
from loguru import logger
from typing import Optional, Tuple


class QueryGuard:
    """SQL注入防护 + 只读限制核心类"""

    # 危险关键字白名单（第一道防线）
    DANGEROUS_KEYWORDS = {
        'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE',
        'GRANT', 'REVOKE', 'EXEC', 'EXECUTE', 'MERGE', 'REPLACE'
    }
    DANGEROUS_KEYWORD_PATTERN = re.compile(
        r"\b(" + "|".join(sorted(DANGEROUS_KEYWORDS)) + r")\b",
        re.IGNORECASE,
    )

    @staticmethod
    def validate_read_only(sql: str, dialect: str = "sqlite") -> Tuple[bool, Optional[str]]:
        """
        严格验证 SQL 是否为只读查询
        返回: (is_safe: bool, error_message: Optional[str])
        """
        if not sql or not sql.strip():
            return False, "SQL 不能为空"

        sql_upper = sql.upper().strip()

        # 第一道防线：快速关键字检查。按词边界匹配，避免误伤 updated_at 等正常字段。
        match = QueryGuard.DANGEROUS_KEYWORD_PATTERN.search(sql_upper)
        if match:
            return False, f"检测到危险关键字: {match.group(1).upper()}，系统只允许 SELECT 查询"

        try:
            # 使用 sqlglot 解析为 AST（抽象语法树）
            # 指定数据库方言
            parsed = sqlglot.parse_one(sql, dialect=dialect)
            if parsed is None:
                return False, "无法解析 SQL 语句"

            # 检查根节点类型：只允许 SELECT 或 WITH (CTE)
            if not isinstance(parsed, (sqlglot.exp.Select, sqlglot.exp.With)):
                return False, f"只允许 SELECT 或 WITH(CTE) 语句，当前类型: {type(parsed).__name__}"

            # 深度遍历 AST，检查所有子节点是否有危险操作 遍历树上每个节点
            for node in parsed.walk():
                node_type = type(node).__name__ # 获取节点类型名
                # node_type 可能是: "Select", "Column", "Table", "Drop", "Delete"...
                if any(danger in node_type for danger in ['Insert', 'Update', 'Delete', 'Drop',
                                                           'Alter', 'Create', 'Truncate', 'Grant']):
                    return False, f"检测到危险操作: {node_type}"

            # 检查是否包含多个语句（用分号分隔）
            # [Select(...), Select(...), Select(...)]
            try:
                statements = sqlglot.split(sql)
            except (AttributeError, TypeError):
                statements = sqlglot.parse(sql)

            if len(statements) > 1:
                return False, "不允许执行多条 SQL 语句"

            return True, None

        except sqlglot.errors.ParseError as e:
            return False, f"SQL 语法解析失败: {str(e)}"
        except Exception as e:
            logger.error(f"Unexpected validation error: {e}")
            return False, f"SQL 验证异常: {str(e)}"

    @staticmethod
    def sanitize_for_execution(sql: str, dialect: str = "sqlite", max_rows: int = 1000) -> str:
        """
        对 SQL 进行安全处理：
        - 自动添加 LIMIT（防止返回过多数据）
        - 格式化 SQL
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect=dialect)
            if parsed and not parsed.find(sqlglot.exp.Limit):   # 找 Limit 节点，找不到返回 None
                parsed = parsed.limit(max_rows)
            return parsed.sql(dialect=dialect, pretty=True)
        except Exception:
            # 如果解析失败，返回原始 SQL（后续执行会失败并被捕获）
            return sql
