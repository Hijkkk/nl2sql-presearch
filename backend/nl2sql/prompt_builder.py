"""
Prompt 构建器 - 让小模型 / 公司内部模型也能较好完成复杂 NL2SQL 的关键
包含：系统指令 + Few-shot 复杂例子 + CoT 思考链 + Schema 格式化
"""
from typing import Dict, List, Any
from loguru import logger


class PromptBuilder:
    def __init__(self):
        self.system_prompt = self._build_system_prompt()
        self.few_shot_examples = self._build_few_shot_examples()

    # LM 的角色设定和严格规则
    #  CoT 思考链 --->强制模型在输出SQL前先进行分步推理。
    #   在SQL之前，先用中文简要说明你的思考步骤（需求分析 → 涉及表 → JOIN逻辑 → 统计方式）。
    #   以及嵌入在 Few-shot 示例的"思考"部分
    def _build_system_prompt(self) -> str:
        return """你是一位资深的数据分析师和SQL专家。
你的任务是根据用户的自然语言问题，结合提供的数据库元数据，生成**只读**的、高质量的SQL查询语句。

## 严格规则（必须遵守）：
1. **只允许生成 SELECT / WITH (CTE) 查询**，绝对禁止任何 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE 等写操作。
2. 优先使用表注释和字段注释理解业务含义。
3. 如果涉及多表，必须使用正确的 JOIN（优先使用外键关系）。
4. 复杂统计必须使用 GROUP BY + 聚合函数（COUNT、SUM、AVG、MAX、MIN）。
5. 支持子查询、窗口函数（ROW_NUMBER、RANK 等）、递归CTE（树形结构）。
6. 结果要易读，合理使用别名（AS）。
7. 最后必须用 ```sql\\nSELECT ... \\n``` 格式输出SQL。
8. 在SQL之前，先用中文简要说明你的思考步骤（需求分析 → 涉及表 → JOIN逻辑 → 统计方式）。

## 支持的高级能力示例：
- 多表JOIN + 条件过滤
- 分组统计 + HAVING
- 子查询 (IN / EXISTS / 标量子查询)
- 窗口函数排名
- 递归查询树形结构
- 模糊匹配 LIKE '%关键词%'

现在开始，根据下面提供的元数据和问题，生成SQL。"""

    # 这部分提供了 3个精心设计的示例，覆盖不同复杂度的查询场景
    # 问题 → 元数据 → 思考过程 → SQL代码块，用于引导模型模仿。
    def _build_few_shot_examples(self) -> str:
        """精心设计的复杂查询 few-shot，提升模型表现（尤其对 ≤7B 或代码模型）"""
        return """
## 示例1：多表JOIN + 聚合 + 子查询
问题：找出技术部中，薪资高于部门平均薪资的员工姓名和薪资
元数据：employees (id, name, salary, department_id), departments (id, name)
思考：
1. 需要 employees 和 departments 两表
2. 先计算技术部平均薪资（子查询）
3. 再JOIN过滤出高于平均的员工
```sql
SELECT e.name, e.salary
FROM employees e
JOIN departments d ON e.department_id = d.id
WHERE d.name = '技术部'
  AND e.salary > (
      SELECT AVG(salary) 
      FROM employees e2 
      JOIN departments d2 ON e2.department_id = d2.id 
      WHERE d2.name = '技术部'
  )
ORDER BY e.salary DESC;
```

## 示例2：三表关联 + 分组统计 + HAVING
问题：统计每个部门的销售总额，只显示总额超过10万的部门，按总额降序
元数据：employees, departments, sales
思考：
1. 三表JOIN：sales -> employees -> departments
2. GROUP BY 部门
3. SUM聚合 + HAVING过滤
```sql
SELECT 
    d.name AS 部门,
    COUNT(DISTINCT e.id) AS 员工数,
    SUM(s.amount) AS 销售总额,
    AVG(s.amount) AS 平均单笔
FROM sales s
JOIN employees e ON s.employee_id = e.id
JOIN departments d ON e.department_id = d.id
GROUP BY d.id, d.name
HAVING SUM(s.amount) > 100000
ORDER BY 销售总额 DESC;
```

## 示例3：窗口函数 + 排名
问题：找出每个部门薪资排名前2的员工
```sql
SELECT 
    d.name AS 部门,
    e.name AS 员工,
    e.salary,
    RANK() OVER (PARTITION BY d.id ORDER BY e.salary DESC) AS 薪资排名
FROM employees e
JOIN departments d ON e.department_id = d.id
WHERE 薪资排名 <= 2
ORDER BY d.name, 薪资排名;
```

现在，请严格按照以上风格和规则，处理用户问题。
"""

    # 构建提示词模板--->
    # prompt = f"""{self.system_prompt}   系统提示词模板
    # {self.few_shot_examples}            例子
    # ## 当前数据库元数据（已精简）：
    # {schema_text}                       CREATE TABLE 风格文本
    # ## 用户问题：
    # {question}                          问题
    # 请一步步思考后生成SQL："""
    def build_prompt(self, question: str, metadata: Dict[str, Any],
                     relevant_tables: List[str] = None) -> str:
        """
        构建完整 Prompt
        relevant_tables: 如果已做表选择，只传入相关表元数据（推荐！减少token）
        """
        schema_text = self._format_metadata(metadata, relevant_tables)

        prompt = f"""{self.system_prompt}

{self.few_shot_examples}

## 当前数据库元数据（已精简）：
{schema_text}

## 用户问题：
{question}

请一步步思考后生成SQL："""
        return prompt

    # 这部分将原始元数据字典转换为模型易理解的 CREATE TABLE 风格文本
    # 表名 + 中文注释
    # 列名、类型、注释（-- 注释）
    # 外键关系
    #     因为
    #     LLM（大语言模型）在训练时见过大量的
    #     CREATE
    #     TABLE
    #     语句，用这种格式喂给它，它能最快、最准确地理解：
    #     有哪些表
    #     每张表有哪些字段、什么类型
    #     表之间的外键关系
    #     这比直接传
    #     JSON
    #     格式效果好得多。
    # 支持通过 relevant_tables 参数精简只保留相关表，减少 token 消耗
    def _format_metadata(self, metadata: Dict[str, Any],
                         relevant_tables: List[str] = None) -> str:
        """将元数据格式化为易读的 CREATE TABLE 风格文本，方便模型理解
                {
            "tables": [
                {
                    "name": "employees",
                    "comment": "员工信息表",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "comment": "主键"},
                        {"name": "name", "type": "TEXT", "comment": ""}
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {"column": "department_id", "ref_table": "departments", "ref_column": "id"}
                    ]
                },
                {
                    "name": "departments",
                    ...
                }
            ],
            "total_tables": 3
        }
        """

        # 提取表名
        tables = metadata.get("tables", [])
        # 遍历，只保存相关表
        if relevant_tables:
            tables = [t for t in tables if t["name"] in relevant_tables]

        lines = []
        for table in tables:
            lines.append(f"### 表: {table['name']} ({table.get('comment', '')})")
            lines.append("CREATE TABLE " + table['name'] + " (")
            # "columns": [
            # {"name": "id", "type": "INTEGER", "comment": "主键"},
            # {"name": "name", "type": "TEXT", "comment": ""}]
            # 遍历每一列的 name, type, comment
            for col in table.get("columns", []):
                comment = f" -- {col.get('comment', '')}" if col.get('comment') else ""
                lines.append(f"    {col['name']} {col['type']}{comment},")

            if table.get("foreign_keys"):
                lines.append("    -- 外键关系:")
                # "foreign_keys": [
                #  {"column": "department_id", "ref_table": "departments", "ref_column": "id"}]
                # 遍历每一个外键关系
                for fk in table["foreign_keys"]:
                    lines.append(f"    --   {fk['column']} -> {fk['ref_table']}({fk['ref_column']})")
            lines.append(");")
            lines.append("")

        if not lines:
            return "（无可用元数据，请检查数据源连接）"

        return "\n".join(lines)
