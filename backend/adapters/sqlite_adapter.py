"""
SQLite 适配器 - 用于快速 Demo 和测试
生产环境可替换为 MySQLAdapter / PostgreSQLAdapter
"""
import sqlite3
from typing import List, Dict, Any, Tuple, Optional
from .base import BaseDataSourceAdapter
from loguru import logger
import os


class SQLiteAdapter(BaseDataSourceAdapter):
    # 这个 ./ 指的是你运行命令时所在的目录，不是代码文件所在的目录。
    def __init__(self, name: str = "sqlite_demo", db_path: str = "./data/demo.db"):
        super().__init__(name)
        self.db_path = db_path
        self._init_demo_data()
        # print(f"当前工作目录：{os.getcwd()}")
        # print(f"数据库路径：{os.path.abspath(db_path)}")

    # 获取数据库方言
    def get_dialect(self) -> str:
        return "sqlite"

    # 初始化演示数据
    def _init_demo_data(self):
        """初始化演示数据（包含复杂查询测试场景）"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # 连接到数据库文件
        conn = sqlite3.connect(self.db_path)
        # 创建一个光标对象，后续所有的 SQL 操作（查询、插入、建表）都要通过它来执行
        cursor = conn.cursor()

        # 创建表结构
        # execute() 执行一条 SQL
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                department_id INTEGER,
                salary REAL,
                hire_date TEXT,
                manager_id INTEGER,
                FOREIGN KEY (department_id) REFERENCES departments(id),
                FOREIGN KEY (manager_id) REFERENCES employees(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                location TEXT,
                budget REAL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY,
                employee_id INTEGER,
                amount REAL,
                sale_date TEXT,
                product_category TEXT,
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )
        """)

        # 如果表为空则插入演示数据
        cursor.execute("SELECT COUNT(*) FROM employees")
        # 取出查询结果的第一行，返回一个元组，如 (0,)
        # 取元组的第一个元素，就是行数 0
        # 判断表是否为空
        if cursor.fetchone()[0] == 0:
            # 部门数据
            # executemany() 用同一句 SQL 模板，批量执行多组数据
            cursor.executemany("""
                INSERT INTO departments (id, name, location, budget) VALUES (?, ?, ?, ?)
            """, [
                (1, '技术部', '北京', 5000000),
                (2, '销售部', '上海', 3000000),
                (3, '人事部', '北京', 800000),
            ])

            # 员工数据（含自关联经理）
            cursor.executemany("""
                INSERT INTO employees (id, name, email, department_id, salary, hire_date, manager_id) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                (1, '张三', 'zhangsan@company.com', 1, 25000, '2020-03-15', None),
                (2, '李四', 'lisi@company.com', 1, 18000, '2021-06-01', 1),
                (3, '王五', 'wangwu@company.com', 2, 22000, '2019-11-20', None),
                (4, '赵六', 'zhaoliu@company.com', 2, 15000, '2022-01-10', 3),
                (5, '钱七', 'qianqi@company.com', 1, 28000, '2018-05-08', 1),
                (6, '孙八', 'sunba@company.com', 3, 12000, '2023-02-28', None),
            ])

            # 销售数据
            cursor.executemany("""
                INSERT INTO sales (id, employee_id, amount, sale_date, product_category) 
                VALUES (?, ?, ?, ?, ?)
            """, [
                (1, 3, 125000, '2024-01-15', '软件'),
                (2, 3, 98000, '2024-02-20', '硬件'),
                (3, 4, 45000, '2024-01-28', '软件'),
                (4, 4, 67000, '2024-03-05', '服务'),
                (5, 2, 32000, '2024-02-10', '软件'),
                (6, 5, 156000, '2024-01-05', '硬件'),
                (7, 3, 89000, '2024-03-12', '软件'),
            ])

        conn.commit()
        conn.close()
        logger.info(f"SQLite demo database initialized at {self.db_path}")

    # 获取数据库元数据
    def get_metadata(self) -> Dict[str, Any]:
        """获取数据库元数据"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # # 第1步：执行查询，找出所有用户创建的表名
        cursor.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name")
        tables = []  # 用于存储表元数据

        # 遍历所有表名
        for (table_name, object_type) in cursor.fetchall():
            # # 第2步：获取表的列信息
            # 用于存储列元数据
            # PRAGMA table_info 是 SQLite 的特殊命令，用来查询表的结构。
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = []
            # 以 employees 表为例，columns 会变成：
            # [
            #     {"name": "id", "type": "INTEGER", "comment": "", "not_null": False, "default": None, "pk": True},
            #     {"name": "name", "type": "TEXT", "comment": "", "not_null": True, "default": None, "pk": False},
            #     {"name": "email", "type": "TEXT", "comment": "", "not_null": False, "default": None, "pk": False},
            #     ...
            # ]
            for col in cursor.fetchall():
                columns.append({
                    "name": col[1],  # 列名，如 "id"
                    "type": col[2],  # 数据类型，如 "INTEGER"
                    "comment": "",  # 注释（SQLite 不支持列注释，所以为空）
                    "not_null": bool(col[3]),  # 是否非空约束
                    "default": col[4],  # 默认值
                    "pk": bool(col[5])  # 是否主键
                })

            # 获取外键信息
            # PRAGMA foreign_key_list 是 SQLite 的特殊命令，用来查询表的外键信息。
            # 以 employees 表为例，fks 会变成：
            # [
            #     {"column": "department_id", "ref_table": "departments", "ref_column": "id"},
            #     {"column": "manager_id", "ref_table": "employees", "ref_column": "id"}
            # ]
            cursor.execute(f"PRAGMA foreign_key_list({table_name})")
            fks = []
            for fk in cursor.fetchall():
                fks.append({
                    "column": fk[3],        # 本表的列名，如 "department_id"
                    "ref_table": fk[2],     # 引用的表名，如 "departments"
                    "ref_column": fk[4]     # 引用的列名，如 "id"
                })

            # {
            #     "name": "employees",
            #     "comment": "员工信息表，包含自关联经理关系",
            #     "columns": [
            #         {"name": "id", "type": "INTEGER", "comment": "", "not_null": False, "default": None, "pk": True},
            #         {"name": "name", "type": "TEXT", "comment": "", "not_null": True, "default": None, "pk": False},
            #         ...
            #     ],
            #     "primary_key": ["id"],
            #     "foreign_keys": [
            #         {"column": "department_id", "ref_table": "departments", "ref_column": "id"},
            #         {"column": "manager_id", "ref_table": "employees", "ref_column": "id"}
            #     ]
            # }
            tables.append({
                "name": table_name,
                "comment": self._get_table_comment(table_name),
                "object_type": "view" if object_type == "view" else "table",
                "columns": columns,
                # 遍历 columns，如果某列的 "pk" 为 True（是主键），就把它的 "name" 取出来，组成一个列表。
                "primary_key": [c["name"] for c in columns if c["pk"]],
                "foreign_keys": fks
            })

        conn.close()
        # {
        #     "tables": [
        #         {"name": "employees", "columns": [...], "primary_key": [...], "foreign_keys": [...]},
        #         {"name": "departments", "columns": [...], "primary_key": [...], "foreign_keys": [...]},
        #         {"name": "sales", "columns": [...], "primary_key": [...], "foreign_keys": [...]}
        #     ],
        #     "total_tables": 3
        # }
        return {"tables": tables, "total_tables": len(tables)}

    # 这个方法的作用是给每张表加一个中文注释，让 LLM 更容易理解表的用途。
    def _get_table_comment(self, table_name: str) -> str:
        comments = {
            "employees": "员工信息表，包含自关联经理关系",
            "departments": "部门信息表",
            "sales": "销售记录表，用于统计分析"
        }
        return comments.get(table_name, "")

    def execute_query(self, sql: str, params: Optional[Dict] = None) -> Tuple[List[Dict], List[str]]:
        """执行查询"""
        conn = sqlite3.connect(self.db_path)       # 打开数据库文件
        # sqlite3.Row 的作用：默认查询结果只能用数字索引访问（row[0]），设置后可以用列名访问（row["name"]），后面 dict(row) 才能正确转换。
        conn.row_factory = sqlite3.Row             # 让查询结果可以按列名访问（而不是只能用索引）
        cursor = conn.cursor()                     # 创建操作光标

        try:
            if params:
                cursor.execute(sql, params)   # 有参数时用参数化查询
            else:
                cursor.execute(sql)           # 没参数时直接执行

            rows = cursor.fetchall()
            # cursor.description 获取列描述信息（列名、类型等）(('id',...), ('name',...))
            # [desc[0] for desc in cursor.description] 从列描述中提取列名 ["id", "name"]
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            # 把每行转成字典 装到列表里面
            results = [dict(row) for row in rows]
            # (
            #     [{"id": 1, "name": "张三"}, {"id": 2, "name": "李四"}],  # results
            #     ["id", "name"]                                            # columns
            # )
            return results, columns

        except Exception as e:
            logger.error(f"SQLite query error: {e}\nSQL: {sql}")
            raise                    # 记录错误日志后，把异常继续向上抛出

        finally:
            conn.close()             # 无论成功还是失败，都关闭连接
