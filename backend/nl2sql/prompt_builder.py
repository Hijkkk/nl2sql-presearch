"""
Prompt 构建器 - 让小模型 / 公司内部模型也能较好完成复杂 NL2SQL 的关键
包含：系统指令 + 数据源专属 Few-shot + Schema 格式化
"""
from typing import Dict, List, Any
from loguru import logger


class PromptBuilder:
    def __init__(self):
        self.system_prompt = self._build_system_prompt()
        self.few_shot_by_source = {
            "sqlite_demo": self._build_few_shot_examples(),
            "postgres_stock": self._build_stock_few_shot_examples(),
            "hive_hadoop_demo": self._build_hadoop_few_shot_examples(),
            "police_address": self._build_police_address_few_shot_examples(),
            "mysql_police_address": self._build_police_address_few_shot_examples(),
            "gauss_ecommerce": self._build_ecommerce_few_shot_examples("orders", "customers"),
            "dameng_ecommerce": self._build_ecommerce_few_shot_examples("ORDERS", "CUSTOMERS"),
            "countries_graphql": self._build_graphql_few_shot_examples(),
            "rest_api_demo": self._build_rest_few_shot_examples(),
        }

    def _few_shots_for(self, data_source: str) -> str:
        return self.few_shot_by_source.get(data_source, self._build_few_shot_examples())

    def _build_graphql_few_shot_examples(self) -> str:
        return """## 示例：国家与洲统计
问题：统计每个洲的国家数量，按数量降序排列
```sql
SELECT c.continent AS 洲, COUNT(*) AS 国家数量
FROM countries c
GROUP BY c.continent
ORDER BY 国家数量 DESC;
```"""

    def _build_rest_few_shot_examples(self) -> str:
        return """## 示例：天气接口结果查询
问题：查询北京的最新天气信息
```sql
SELECT city AS 城市, weather AS 天气, temperature AS 温度, reporttime AS 发布时间
FROM amap_weather
WHERE city LIKE '%北京%'
ORDER BY reporttime DESC
LIMIT 1;
```"""

    @staticmethod
    def _build_ecommerce_few_shot_examples(orders: str, customers: str) -> str:
        return f"""## 示例：电商客户消费统计
问题：列出消费金额最高的前 10 名客户
```sql
SELECT c.customer_name AS 客户名称, SUM(o.total_amount) AS 消费总额
FROM {orders} o
JOIN {customers} c ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.customer_name
ORDER BY 消费总额 DESC
LIMIT 10;
```"""

    def _build_system_prompt(self) -> str:
        return """你是一位资深的数据分析师和SQL专家。
你的任务是根据用户的自然语言问题，结合提供的数据库元数据，生成**只读**的、高质量的SQL查询语句。

## 严格规则（必须遵守）：
1. **只允许生成 SELECT / WITH (CTE) 查询**，绝对禁止任何 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE 等写操作。
2. 优先使用表注释和字段注释理解业务含义。
3. 如果涉及多表，必须使用正确的 JOIN（优先使用外键关系）。
4. 复杂统计必须使用 GROUP BY + 聚合函数（COUNT、SUM、AVG、MAX、MIN）。
5. 支持子查询、窗口函数（ROW_NUMBER、RANK 等）、递归CTE（树形结构）。
6. 结果要易读，必须合理使用中文别名（AS）。如果用户使用中文提问，SELECT 列表中的业务字段应尽量输出中文列名，例如 trade_date AS 交易日期、open_price AS 开盘价、close_price AS 收盘价、volume AS 成交量。
7. 不要臆造元数据中不存在的字段。比如股票行情表只有 symbol 时，只能返回股票代码；如果没有股票名称/中文名字段，不要生成 stock_name、股票名称 等不存在的列。
8. 只输出一条 SQL，可以使用 ```sql 代码块，也可以直接输出裸 SQL。
9. 不要输出解释、推理过程、自然语言前缀或多余文本。

## 支持的高级能力示例：
- 多表JOIN + 条件过滤
- 分组统计 + HAVING
- 子查询 (IN / EXISTS / 标量子查询)
- 窗口函数排名
- 递归查询树形结构
- 模糊匹配 LIKE '%关键词%'

现在开始，根据下面提供的元数据和问题，只生成 SQL。"""

    # 这部分提供了 3个精心设计的示例，覆盖不同复杂度的查询场景
    # 问题 → 元数据 → 思考过程 → SQL代码块，用于引导模型模仿。
    def _build_few_shot_examples(self) -> str:
        """精心设计的复杂查询 few-shot，提升模型表现（尤其对 ≤7B 或代码模型）"""
        return """
## 示例1：多表JOIN + 聚合 + 子查询
问题：找出技术部中，薪资高于部门平均薪资的员工姓名和薪资
元数据：employees (id, name, salary, department_id), departments (id, name)
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
    ranked.部门,
    ranked.员工,
    ranked.salary,
    ranked.薪资排名
FROM (
    SELECT 
        d.name AS 部门,
        e.name AS 员工,
        e.salary,
        RANK() OVER (PARTITION BY d.id ORDER BY e.salary DESC) AS 薪资排名
    FROM employees e
    JOIN departments d ON e.department_id = d.id
) ranked
WHERE ranked.薪资排名 <= 2
ORDER BY ranked.部门, ranked.薪资排名;
```

## 示例4：模糊匹配
问题：查询名称里包含“技术”的部门
```sql
SELECT id, name, location
FROM departments
WHERE name LIKE '%技术%'
ORDER BY id;
```

现在，请严格按照以上风格和规则，只输出 SQL。
"""

    # 构建提示词模板--->
    # prompt = f"""{self.system_prompt}   系统提示词模板
    # {self.few_shot_examples}            例子
    # ## 当前数据库元数据（已精简）：
    # {schema_text}                       CREATE TABLE 风格文本
    # ## 用户问题：
    # {question}                          问题
    # 请一步步思考后生成SQL："""
    def _build_stock_few_shot_examples(self) -> str:
        return """

## 示例5：股票中文名 + 行情表 JOIN 证券主数据
问题：苹果公司今天的收盘价是多少？
元数据：stock_daily_prices(symbol, trade_date, close_price), stock_symbols(symbol, chinese_name, security_name)
```sql
SELECT
    s.chinese_name AS 公司名称,
    p.symbol AS 股票代码,
    p.trade_date AS 交易日期,
    p.close_price AS 收盘价
FROM stock_daily_prices p
JOIN stock_symbols s ON p.symbol = s.symbol
WHERE s.chinese_name = '苹果公司'
   OR s.security_name ILIKE '%Apple%'
ORDER BY p.trade_date DESC
LIMIT 1;
```

## 示例6：杠杆 ETF + ETP 元数据 JOIN 证券主数据
问题：哪些杠杆 ETF？显示代码、名称、杠杆倍数和底层资产
元数据：stock_etp_metadata(symbol, etp_type, leveraged_flag, leveraged_ratio, underlying_asset), stock_symbols(symbol, security_name, chinese_name)
```sql
SELECT
    e.symbol AS 股票代码,
    COALESCE(s.chinese_name, s.security_name) AS 证券名称,
    e.etp_type AS ETP类型,
    e.leveraged_ratio AS 杠杆倍数,
    e.underlying_asset AS 底层资产
FROM stock_etp_metadata e
JOIN stock_symbols s ON e.symbol = s.symbol
WHERE e.leveraged_flag = TRUE
ORDER BY e.leveraged_ratio DESC NULLS LAST, e.symbol
LIMIT 50;
```

## 示例7：SIC 行业树 + 自关联 / 递归 CTE
问题：SIC 3571 行业以及它的上级行业是什么？
元数据：stock_industry_classification(sic_code, sic_name, chinese_name, parent_sic_code)
```sql
WITH RECURSIVE sic_tree AS (
    SELECT
        sic_code,
        sic_name,
        chinese_name,
        parent_sic_code,
        0 AS level
    FROM stock_industry_classification
    WHERE sic_code = '3571'

    UNION ALL

    SELECT
        parent.sic_code,
        parent.sic_name,
        parent.chinese_name,
        parent.parent_sic_code,
        child.level + 1 AS level
    FROM stock_industry_classification parent
    JOIN sic_tree child ON child.parent_sic_code = parent.sic_code
)
SELECT
    sic_code AS SIC代码,
    COALESCE(chinese_name, sic_name) AS 行业名称,
    parent_sic_code AS 上级SIC代码,
    level AS 层级
FROM sic_tree
ORDER BY level;
```
"""


    def _build_hadoop_few_shot_examples(self) -> str:
        return """

## 示例8：Hadoop/HDFS 星型模型 + 城市 GMV
问题：每个城市销售总额 Top 5 是哪些？
元数据：hadoop_order_events(event_id, event_date, user_id, product_id, region_id, order_count, gmv), hadoop_region_dim(region_id, province, city, city_tier, region_group)
```sql
SELECT
    r.city AS 城市,
    ROUND(SUM(e.gmv), 2) AS 成交金额
FROM hadoop_order_events e
JOIN hadoop_region_dim r ON e.region_id = r.region_id
GROUP BY r.city
ORDER BY 成交金额 DESC
LIMIT 5;
```

## 示例9：Hadoop/HDFS 星型模型 + 品牌月度销量趋势
问题：各品牌每月销量趋势是什么？
元数据：hadoop_order_events(event_date, product_id, order_count, gmv), hadoop_product_dim(product_id, brand, product_name, category)
```sql
SELECT
    p.brand AS 品牌,
    substr(e.event_date, 1, 7) AS 月份,
    SUM(e.order_count) AS 销量
FROM hadoop_order_events e
JOIN hadoop_product_dim p ON e.product_id = p.product_id
GROUP BY p.brand, substr(e.event_date, 1, 7)
ORDER BY p.brand, 月份;
```
"""

    def _build_police_address_few_shot_examples(self) -> str:
        return """

## 示例10：警务地址库中同名词的消歧
问题：查询包含张三的地址别名记录
元数据：addr_alias(alias_name, std_address_id), addr_standard_address(std_address_id, full_address)
```sql
SELECT
    aa.alias_name AS 地址别名,
    sa.full_address AS 标准地址
FROM addr_alias aa
JOIN addr_standard_address sa ON aa.std_address_id = sa.std_address_id
WHERE aa.alias_name LIKE '%张三%'
ORDER BY aa.id DESC
LIMIT 100;
```

## 示例11：警务地址库中按人员姓名查询住址
问题：查询姓名叫张三的人员当前住在哪些地址
元数据：person_basic(person_id, name), person_address_relation(person_id, house_id, is_current), house_info(house_id, std_address_id), addr_standard_address(std_address_id, full_address)
```sql
SELECT
    p.name AS 姓名,
    p.person_id AS 人员编号,
    sa.full_address AS 当前住址
FROM person_basic p
JOIN person_address_relation par ON p.person_id = par.person_id
JOIN house_info h ON par.house_id = h.house_id
JOIN addr_standard_address sa ON h.std_address_id = sa.std_address_id
WHERE p.name = '张三'
  AND par.is_current = 1
ORDER BY p.person_id
LIMIT 100;
```

## 示例12：警情内容中的关键词检索
问题：统计报警内容中提到张三的警情数量
元数据：police_alert(alert_no, alert_time, alert_content, alert_address)
```sql
SELECT
    COUNT(*) AS 警情数量
FROM police_alert
WHERE alert_content LIKE '%张三%';
```
"""
    def build_prompt(self, question: str, metadata: Dict[str, Any],
                     relevant_tables: List[str] = None, data_source: str = "") -> str:
        """
        构建完整 Prompt
        relevant_tables: 如果已做表选择，只传入相关表元数据（推荐！减少token）
        """
        schema_text = self._format_metadata(metadata, relevant_tables)

        prompt = f"""{self.system_prompt}

{self._few_shots_for(data_source)}

## 当前数据库元数据（已精简）：
{schema_text}

## 用户问题：
{question}

只输出 SQL："""
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
    def build_xiyan_prompt(self, question: str, metadata: Dict[str, Any], dialect: str = "SQLite",
                           relevant_tables: List[str] = None) -> str:
        """Build the concise XiYanSQL prompt recommended by the model card."""
        schema_text = self._format_metadata(metadata, relevant_tables)
        evidence_text = "只允许生成 SELECT 或 WITH 查询；不得编造不存在的表或字段；按当前数据源方言生成 SQL；只输出 SQL，不输出解释、思考过程或 Markdown 外文本。"
        return f"""你是一名{dialect}专家，现在需要阅读并理解下面的【数据库schema】描述，以及可能用到的【参考信息】，并运用{dialect}知识生成sql语句回答【用户问题】。
【用户问题】
{question}

【数据库schema】
{schema_text}

【参考信息】
{evidence_text}

【用户问题】
{question}

```sql"""

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
            table_comment = table.get("summary") or table.get("comment", "")
            lines.append(f"### 表: {table['name']} ({table_comment})")
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
