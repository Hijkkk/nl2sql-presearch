"""
MySQL 适配器 - 生产环境可直接替换 SQLiteAdapter 使用
使用 PyMySQL + 原生实现（也可后续升级为 SQLAlchemy）
"""
import pymysql
from typing import List, Dict, Any, Tuple, Optional
from .base import BaseDataSourceAdapter
from loguru import logger


class MySQLAdapter(BaseDataSourceAdapter):
    def __init__(
        self,
        name: str = "mysql_demo",
        host: str = "localhost",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "nl2sql_demo",
        charset: str = "utf8mb4"
    ):
        super().__init__(name)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.charset = charset
        self._conn = None

    def get_dialect(self) -> str:
        return "mysql"

    def _get_connection(self):
        """获取数据库连接（简单实现，生产建议用连接池）"""
        if self._conn is None or not self._conn.open:
            if not self.database:
                raise ValueError("MySQL 查询数据源未配置数据库名 MYSQL_QUERY_DATABASE")
            self._conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset=self.charset,
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True
            )
        return self._conn

    def ping(self) -> None:
        """检查 MySQL 数据源连接是否可用。"""
        conn = self._get_connection()
        conn.ping(reconnect=True)

    def get_metadata(self) -> Dict[str, Any]:
        """获取 MySQL 元数据（表、字段、外键、注释）"""
        conn = self._get_connection()
        cursor = conn.cursor()

        # 获取所有表
        # information_schema 是 MySQL 内置的"元数据库"，记录了所有数据库对象的信息。
        cursor.execute("""
            SELECT TABLE_NAME, TABLE_COMMENT 
            FROM information_schema.TABLES 
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """, (self.database,))
        tables_info = cursor.fetchall()
        # | TABLE_NAME  | TABLE_COMMENT    |
        # |-------------|------------------|
        # | departments | 部门信息表        |
        # | employees   | 员工信息表        |


        tables = []
        for t in tables_info:
            table_name = t["TABLE_NAME"]
            table_comment = t["TABLE_COMMENT"] or ""

            # 获取列信息
            # | COLUMN_NAME   | COLUMN_TYPE   | IS_NULLABLE | COLUMN_DEFAULT | COLUMN_KEY | COLUMN_COMMENT |
            # |---------------|---------------|-------------|----------------|------------|----------------|
            # | id            | int           | NO          | NULL           | PRI        | 员工ID          |
            # | name          | varchar(100)  | YES         | NULL           |            | 姓名            |
            # | department_id | int           | YES         | NULL           |            | 所属部门         |
            # | manager_id    | int           | YES         | NULL           |            | 上级经理         |
            cursor.execute("""
                SELECT 
                    COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, 
                    IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, COLUMN_COMMENT
                FROM information_schema.COLUMNS 
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
            """, (self.database, table_name))
            columns_raw = cursor.fetchall()



            columns = []
            primary_key = []
            for col in columns_raw:
                columns.append({
                    "name": col["COLUMN_NAME"],
                    "type": col["COLUMN_TYPE"],
                    "comment": col["COLUMN_COMMENT"] or "",
                    "not_null": col["IS_NULLABLE"] == "NO",
                    "default": col["COLUMN_DEFAULT"],
                    "pk": col["COLUMN_KEY"] == "PRI"
                })
                if col["COLUMN_KEY"] == "PRI":
                    primary_key.append(col["COLUMN_NAME"])
            # columns = [
            #     {"name": "id",            "type": "int",          "comment": "员工ID",   "not_null": True,  "default": None, "pk": True},
            #     {"name": "name",          "type": "varchar(100)", "comment": "姓名",     "not_null": False, "default": None, "pk": False},
            #     {"name": "department_id", "type": "int",          "comment": "所属部门", "not_null": False, "default": None, "pk": False},
            #     {"name": "manager_id",    "type": "int",          "comment": "上级经理", "not_null": False, "default": None, "pk": False},
            # ]
            # primary_key = ["id"]   # COLUMN_KEY == "PRI" 的列

            # 获取外键
            cursor.execute("""
                SELECT 
                    COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s 
                  AND TABLE_NAME = %s 
                  AND REFERENCED_TABLE_NAME IS NOT NULL
            """, (self.database, table_name))
            fks_raw = cursor.fetchall()

            # foreign_keys = [
            #     {"column": "department_id", "ref_table": "departments", "ref_column": "id"}
            # ]
            foreign_keys = [
                {
                    "column": fk["COLUMN_NAME"],
                    "ref_table": fk["REFERENCED_TABLE_NAME"],
                    "ref_column": fk["REFERENCED_COLUMN_NAME"]
                }
                for fk in fks_raw
            ]

            tables.append({
                "name": table_name,
                "comment": table_comment,
                "columns": columns,
                "primary_key": primary_key,
                "foreign_keys": foreign_keys
            })

        cursor.close()
        return {"tables": tables, "total_tables": len(tables)}

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        """执行只读查询"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)

            #     游标类型
            #     fetchall() 结果

            #     普通 Cursor
            #     [(1, '张三', 1), (2, '李四', 2)] — 只有值，没有列名

            #     DictCursor
            #     [{"id": 1, "name": "张三", ...}] — 带列名的字典

            # results = [
            #     {"id": 1, "name": "张三", "department_id": 1},
            #     {"id": 2, "name": "李四", "department_id": 2},
            #     {"id": 3, "name": "王五", "department_id": 1},
            # ]
            results = cursor.fetchall()  # 已经是 list[dict]
            # 例如执行了 SELECT id, name FROM employees 后
            # cursor.description = [
            #     ('id', 3, None, 11, None, None, 0),
            #     ('name', 253, None, 300, None, None, 1),
            # ]
            # desc[0] 就是取每个元组的第一个元素（列名）
            # columns = ["id", "name"]
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return results, columns
        except Exception as e:
            logger.error(f"MySQL query error: {e}\nSQL: {sql}")
            raise
        finally:
            cursor.close()

    def close(self):
        if self._conn and self._conn.open:
            self._conn.close()
