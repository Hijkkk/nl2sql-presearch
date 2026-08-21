"""
Prompt 构建器 - 让小模型 / 公司内部模型也能较好完成复杂 NL2SQL 的关键
包含：系统指令 + 数据源专属 Few-shot + Schema 格式化

本文件在工程里的位置（看 backend/agent/graph.py 工作流）：

  Chat 路径（HTTP API 直接问一句）        : build_xiyan_prompt()          ← 长版含 few-shot
  受控 Agent 路径 (LangGraph 六节点)      : build_controlled_xiyan_prompt() ← 精简版给本地 3B
  共享业务规则（chat/agent 都要用）       : get_agent_source_template()     ← 同一个 source_id 一份
                                          ↑ 三处都从这里读 sql_rules，保证两条路径语义一致

调用方：
  - backend/nl2sql/sql_generator.py:284 generate_controlled_sql()  调 build_controlled_xiyan_prompt()
  - backend/nl2sql/sql_generator.py:204 generate()                 调 build_xiyan_prompt()
  - backend/agent/llm.py:443  QwenPlanner                           调 get_agent_source_template()
  - backend/agent/llm.py:358  QwenPlanReviewer                       调 get_agent_source_template()

为什么必须分"长版/精简版"两套？见 build_controlled_xiyan_prompt() 的 docstring。
"""
from typing import Dict, List, Any, TYPE_CHECKING

from backend.config.config import settings
from loguru import logger
from backend.nl2sql.catalog import catalog_prompt_hint

if TYPE_CHECKING:
    from backend.agent.contracts import XiYanPromptContext


class PromptBuilder:
    """项目里唯一的 prompt 工厂。所有 prompt 模板、few-shot、业务规则都集中在这里维护。

    设计原则：
      1) 系统提示（"你是 SQL 专家…"）只写一次，所有路径共享
      2) Few-shot 按数据源分桶，调用时按 source_id 查表
      3) 业务规则（半开日期、最新价视图、地址别名 LIKE…）走 get_agent_source_template()，
         chat 路径和受控 agent 路径都从同一个字典里取，避免"两处维护、语义漂移"
    """

    def __init__(self):
        # 把"系统指令"和"每个数据源的 few-shot"在构造时就拼好缓存起来
        # 后续每次请求只需要做轻量拼接，不必重复拼长字符串
        self.system_prompt = self._build_system_prompt()

        # 数据源 → 各自专属的 few-shot 示例文本
        # 加新数据源时：写一个 _build_xxx_few_shot_examples()，再在这里登记即可
        self.few_shot_by_source = {
            "sqlite_demo":          self._build_few_shot_examples(),                  # 通用业务示例
            "postgres_stock":       self._build_stock_few_shot_examples(),            # 股票/证券
            "hive_hadoop_demo":     self._build_hadoop_few_shot_examples(),           # Hadoop 星型模型
            "police_address":       self._build_police_address_few_shot_examples(),   # 警务地址
            "mysql_police_address": self._build_police_address_few_shot_examples(),   # 同上，MySQL 版
            "gauss_ecommerce":      self._build_ecommerce_few_shot_examples("orders", "customers"),  # 电商（gauss）
            "dameng_ecommerce":     self._build_ecommerce_few_shot_examples("ORDERS", "CUSTOMERS"),  # 电商（达梦，大写）
            "countries_graphql":    self._build_graphql_few_shot_examples(),          # GraphQL 虚拟表
            "rest_api_demo":        self._build_rest_few_shot_examples(),             # REST 虚拟表
        }

    def _few_shots_for(self, data_source: str) -> str:
        """按 source_id 找对应的 few-shot；找不到就用通用版本兜底。"""
        # 默认是通用提示词
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
        """生成通用的"系统提示词"——所有数据源、所有路径共用这一份。

        重点约束：
          - 只允许 SELECT/WITH（只读），绝对禁止写操作
          - 字段必须来自元数据，禁止臆造（防"幻觉"列）
          - 中文问题 → 中文别名（业务字段输出中文）
          - 只输出一条 SQL，不输出解释
        """
        return """你是一位资深的数据分析师和SQL专家。
你的任务是根据用户的自然语言问题，结合提供的数据库元数据，生成**只读**的、高质量的SQL查询语句。

## 严格规则（必须遵守）：
1. **只允许生成 SELECT / WITH (CTE) 查询**，绝对禁止任何 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE 等写操作。
2. 优先使用表注释和字段注释理解业务含义。
3. 如果涉及多表，必须使用正确的 JOIN（优先使用外键关系）。
4. 复杂统计必须使用 GROUP BY + 聚合函数（COUNT、SUM、AVG、MAX、MIN）。
5. 支持子查询、窗口函数（ROW_NUMBER、RANK 等）、递归CTE（树形结构）。
6. 结果要易读，必须合理使用中文别名（AS）。如果用户使用中文提问，SELECT 列表中的业务字段应尽量输出中文列名，例如 trade_date AS 交易日期、open_price AS 开盘价、close_price AS 收盘价、volume AS 成交量。
7. 不要臆造元数据中不存在的字段。比如股票行情表只有 symbol 时，只能返回股票代码；如果没有股票名称/中文名字段，不要生成 stock_name、股票名称 等不存在的列。字段必须属于其 FROM/JOIN 后的对象：视图专属别名只能用于该视图，不能写到同名业务基表上；基表字段也不能假定存在于视图中。
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

    # 旧版模板草稿（已废弃，保留仅为参考）：
    # 构建提示词模板--->
    # prompt = f"""{self.system_prompt}   系统提示词模板
    # {self.few_shot_examples}            例子
    # ## 当前数据库元数据（已精简）：
    # {schema_text}                       CREATE TABLE 风格文本
    # ## 用户问题：
    # {question}                          问题
    # 请一步步思考后生成SQL："""
    def _build_stock_few_shot_examples(self) -> str:
        """股票/证券场景的 few-shot：覆盖"中文名+行情 JOIN 证券主数据"、"杠杆 ETP"、"递归 CTE 行业树"。

        选取理由：
          - 中文名查询：股票领域最常见入口（用户说"苹果公司"而非"AAPL"）
          - ETP 元数据：演示 LEFT JOIN 多个元数据表
          - 递归 CTE：演示 WITH RECURSIVE，对应 SIC 行业树这种典型层级场景
        """
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
        """Hadoop/HDFS 星型模型场景的 few-shot：覆盖"城市 GMV TopN"和"品牌月度趋势"。

        关键提醒（同时会在 get_agent_source_template() 的 sql_rules 里再写一次）：
          - 日期是 TEXT 类型 YYYY-MM-DD，按月用 substr(event_date, 1, 7)
          - 不能用 TO_DATE / DATE_FORMAT / date_trunc（Hive 函数）
        """
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
        """警务地址库场景的 few-shot：覆盖"地址别名消歧"、"人员当前住址"、"警情内容检索"。

        注意：mysql_police_address 和 police_address 都共用这一份 few-shot，
              但 get_agent_source_template() 里两者拿到的 sql_rules 略有差异。
        """
        return """

## 示例10：警务地址库中同名词的消歧
问题：查询包含张三的地址别名记录
元数据：addr_alias(alias_name, std_address_code), addr_standard_address(std_address_code, full_address)
```sql
SELECT
    aa.alias_name AS 地址别名,
    sa.full_address AS 标准地址
FROM addr_alias aa
JOIN addr_standard_address sa ON aa.std_address_code = sa.std_address_code
WHERE aa.alias_name LIKE '%张三%'
ORDER BY aa.alias_id DESC
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
        【老接口】早期通用 prompt 模板，已被 build_xiyan_prompt() 替代。
        保留原因：部分老测试和老接口还在调，迁移完所有调用方后再删。

        relevant_tables: 如果已做表选择，只传入相关表元数据（推荐！减少token）
        """
        schema_text = self._format_metadata(metadata, relevant_tables)
        catalog_hint = catalog_prompt_hint(
            data_source,
            [table.get("name", "") for table in metadata.get("tables", [])],
        )
        catalog_section = f"\n## Catalog hints\n{catalog_hint}\n" if catalog_hint else ""

        prompt = f"""{self.system_prompt}

{self._few_shots_for(data_source)}

## 当前数据库元数据（已精简）：
{schema_text}
{catalog_section}

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
                           relevant_tables: List[str] = None, data_source: str = "",
                           controlled_context: "XiYanPromptContext | None" = None) -> str:
        """Chat 路径的主 prompt 构造器。被 sql_generator.generate() 调用。

        输入：
          - question      : 用户原始问题
          - metadata      : retrieve 阶段拿到的完整元数据（含 tables / schema_signature）
          - dialect       : SQL 方言（默认 SQLite，postgres / mysql / hive 各有差异）
          - relevant_tables: 可选白名单，只把相关表塞进 schema_text（省 token）
          - data_source   : 数据源 ID，用于查 few-shot 和源专属业务规则
          - controlled_context: 可选；如果是 Agent 受控调用，把"受控边界"写到 prompt 头部

        输出格式（中文 prompt 模板）：
          1) "你是 XXX 专家…" 角色设定
          2) 【受控执行上下文】（仅 controlled_context 非空时）
          3) 【用户问题】
          4) 【数据库 schema】        ← _format_metadata() 拼成 CREATE TABLE 风格
          5) 【参考信息】            ← 通用规则 + data_source 专属 sql_rules
          6) ```sql                  ← 引导模型在代码块里写 SQL

        与 build_controlled_xiyan_prompt() 的差别：本函数保留中文角色设定和详细 few-shot，
        适合云端大模型（DashScope Qwen）调用；那个是英文精简版，给本地 3B 用。
        """
        schema_text = self._format_metadata(metadata, relevant_tables)
        catalog_hint = catalog_prompt_hint(
            data_source,
            [table.get("name", "") for table in metadata.get("tables", [])],
        )
        evidence_items = [
            "只允许生成 SELECT 或 WITH 查询；不得编造不存在的表或字段；按当前数据源方言生成 SQL；只输出 SQL，不输出解释、思考过程或 Markdown 外文本。",
        ]
        if catalog_hint:
            evidence_items.append(catalog_hint)
        # ↓↓↓ 以下是"数据源专属业务规则"——每加一个新数据源，新增一个 elif 分支
        # 这些规则在 chat 路径是辅助说明，但在受控 agent 路径是被当作硬约束
        if data_source == "hive_hadoop_demo":
            evidence_items.append(
                "当前 Hadoop/HDFS 演示源由本地 SQLite 执行 CSV 表；日期是 TEXT 类型 YYYY-MM-DD。按月统计必须使用 substr(event_date, 1, 7)，不要使用 TO_DATE、DATE_FORMAT、date_trunc 等 Hive/MySQL/PostgreSQL 函数。"
            )
            evidence_items.append(
                "当用户询问“各品牌每月销量趋势”时，必须 SELECT p.brand、substr(e.event_date, 1, 7)、SUM(e.order_count)，FROM hadoop_order_events e JOIN hadoop_product_dim p ON e.product_id = p.product_id，并按 p.brand 和 substr(e.event_date, 1, 7) 分组。"
            )
            evidence_items.append(
                "用户问销售额高于所有城市平均销售额的城市时，先按城市聚合 SUM(e.gmv) 得到 city_gmv，再用 CTE 或子查询计算 AVG(city_gmv) 比较；不能把单笔 e.gmv 的 AVG 当作城市销售额平均值。"
            )
        elif data_source == "postgres_stock":
            evidence_items.append(
                "v_stock_latest_price 没有 sector_code；科技股筛选必须 JOIN stock_symbols s ON s.symbol = l.symbol AND s.exchange_code = l.exchange_code，并使用 s.sector_code = 'TECHNOLOGY'。v_stock_price_detail 才包含 sector_code。"
            )
            evidence_items.append(
                "比较最新收盘价与自身 2026 年 7 月平均收盘价时，使用相关子查询，并同时按 symbol 和 exchange_code 关联历史行情；日期用 [2026-07-01, 2026-08-01) 半开区间。"
            )
        elif data_source == "sqlite_demo":
            evidence_items.append(
                "涉及某年的 sale_date、hire_date 等日期筛选，必须使用半开区间，例如 2026 年使用字段 >= '2026-01-01' AND 字段 < '2027-01-01'；不要使用 STRFTIME('%Y', 字段)、YEAR(字段) 等会影响索引使用的函数。"
            )
            evidence_items.append(
                "统计各部门销售额必须 sales s JOIN employees e ON e.id = s.employee_id JOIN departments d ON d.id = e.department_id，返回 d.name AS department_name，不能把 departments.id 错连到 sales.employee_id。题目要求包含没有员工的部门时，必须从 departments d LEFT JOIN employees e 出发，并返回 d.name 而不是 department_id。"
            )
        elif data_source in {"mysql_police_address", "police_address"}:
            evidence_items.append(
                "查询地址别名及对应标准地址时，必须从 addr_alias 关联 addr_standard_address；使用 JOIN addr_standard_address sa ON aa.std_address_code = sa.std_address_code，并返回 aa.alias_name 与 sa.full_address。不要使用 addr_alias.std_address_id，因为该字段不存在。"
            )
            evidence_items.append(
                "当用户要求地址别名“包含 X”时，必须提取用户问题中的实际词 X，在 addr_alias.alias_name 上使用 LIKE '%X%' 过滤；不要把“关键词”或“X”作为字面量输出。例如包含人民路时使用 aa.alias_name LIKE '%人民路%'。"
            )
            evidence_items.append(
                "查询当前登记居住人员优先使用 v_nl2sql_person_current_address；查询出租房和当前登记居住人数优先使用 v_nl2sql_house_occupancy。这两个视图已经是当前口径，不存在 is_current 字段，不要再添加 is_current = 1。v_nl2sql_person_current_address.residential_code 是编码，不是小区名称；用户说“和平里小区”等小区名时，用 full_address LIKE '%和平里小区%' 过滤。v_nl2sql_house_occupancy 也没有 residential_name 字段，小区名同样用 full_address LIKE。"
            )
            evidence_items.append(
                "警务报警统计优先使用 v_nl2sql_alert_detail。日期月份必须用半开区间，例如 2026年1月 使用 alert_time >= '2026-01-01 00:00:00' AND alert_time < '2026-02-01 00:00:00'，不要使用 YEAR(alert_time) 或 MONTH(alert_time)。用户说“治安报警”时，字段实际值是“治安案件报警”，必须使用 alert_type_name LIKE '%治安%'，不要使用 alert_type_name = '治安'。用户说“已结案”时使用 alert_status_name = '已结案'，不要把中文状态写到 alert_status_code。"
            )
            evidence_items.append(
                "查询报警涉及人员、嫌疑人、受害人、证人时必须 JOIN alert_involvement ai ON pa.alert_no = ai.alert_no。alert_involvement 的姓名字段是 ai.name，不是 ai.person_name；角色字段是 ai.role_code，不是 ai.role_type。嫌疑人使用 ai.role_code = 'SUSPECT'，受害人 VICTIM，证人 WITNESS，亲属 RELATIVE，一般涉及人员 INVOLVED。"
            )
            evidence_items.append(
                "“和平里小区当前登记了多少名居住人员”统计该小区所有当前登记人员，不得额外限制 house_relation_name = '本人'；使用 COUNT(DISTINCT person_code) FROM v_nl2sql_person_current_address WHERE full_address LIKE '%和平里小区%'。出租房问题返回 house_code、full_address、current_person_count，并按 current_person_count DESC, house_code 排序。已结案但未关联案事件的核查必须 LEFT JOIN alert_event e ON e.alert_no = a.alert_no，条件为 a.alert_status_code = 'CLOSED' AND e.alert_no IS NULL。"
            )
            evidence_items.append(
                "2026 年 1 月东城区已结案的治安报警，必须直接查询 v_nl2sql_alert_detail，使用 COUNT(DISTINCT alert_no)、district_name = '东城区'、alert_type_code = 'SECURITY'、alert_status_code = 'CLOSED'，以及 alert_time >= '2026-01-01 00:00:00' AND alert_time < '2026-02-01 00:00:00'；不需要关联 police_alert 或 alert_event。"
            )
        elif data_source == "gauss_ecommerce":
            evidence_items.append(
                "用户问“各城市/所有城市/全部城市 + 已完成订单金额”时，必须返回所有客户城市，包括没有已完成订单的城市；从 customers c 出发 LEFT JOIN orders o，并把 o.status = 'COMPLETED' 与年份范围条件写在 ON 子句中，金额用 COALESCE(SUM(o.total_amount), 0)，不要把订单条件写在 WHERE 中导致无订单城市被过滤。"
            )
            evidence_items.append(
                "v_order_summary 的城市字段是 customer_city；customers 基表的字段是 city。使用视图时写 customer_city，使用 customers c 基表时必须写 c.city，绝不能写 c.customer_city。"
            )
        elif data_source == "dameng_ecommerce":
            evidence_items.append(
                "用户问“各城市/所有城市/全部城市 + 已完成订单金额”时，必须返回所有客户城市，包括没有已完成订单的城市；从 CUSTOMERS C 出发 LEFT JOIN ORDERS O，并把 O.STATUS = 'COMPLETED' 与年份范围条件写在 ON 子句中，金额用 COALESCE(SUM(O.TOTAL_AMOUNT), 0)，不要把订单条件写在 WHERE 中导致无订单城市被过滤。"
            )
            evidence_items.append(
                "V_ORDER_SUMMARY 的城市字段是 CUSTOMER_CITY；CUSTOMERS C 基表的字段是 CITY。使用视图时写 CUSTOMER_CITY，使用基表时必须写 C.CITY，绝不能写 C.CUSTOMER_CITY。"
            )
        # The source profile is also supplied to Agent planning and controlled
        # XiYan generation.  Keep the detailed Chat-only few-shots above, while
        # sharing the compact semantic rules across both execution paths.
        evidence_items.extend(self.get_agent_source_template(data_source)["sql_rules"])
        evidence_text = "\n".join(evidence_items)
        controlled_header = ""
        if controlled_context:
            allowed_objects = ", ".join(controlled_context.schema_closure_object_ids)
            allowed_fields = ", ".join(controlled_context.allowed_field_ids) or "当前 schema 中列出的字段"
            controlled_header = f"""\n【受控执行上下文】
当前数据源 ID：{controlled_context.source_id}
当前方言：{controlled_context.dialect}
Schema 版本：{controlled_context.schema_signature or '未提供'}
允许对象：{allowed_objects}
允许字段：{allowed_fields}
执行限制：仅一条 SELECT/WITH，最多返回 {controlled_context.max_rows} 行；不得跨数据源、调用 API 或使用未列出的对象/字段。
"""
        return f"""你是一名{dialect}专家，现在需要阅读并理解下面的【数据库schema】描述，以及可能用到的【参考信息】，并运用{dialect}知识生成sql语句回答【用户问题】。
{controlled_header}
【用户问题】
{question}

【数据库schema】
{schema_text}

【参考信息】
{evidence_text}

【用户问题】
{question}

```sql"""

    def build_controlled_xiyan_prompt(
        self,
        context: "XiYanPromptContext",
        metadata: Dict[str, Any],
    ) -> str:
        """受控 Agent 路径的精简 prompt 构造器。被 sql_generator.generate_controlled_sql() 调用。

        为什么必须精简？
          chat 路径用云端大模型（Qwen3-Max），上下文充裕，可以塞完整 few-shot 和中文角色设定；
          受控 agent 路径跑的是本地 XiYan-3B Q4 量化（Ollama 部署），上下文只有 ~4K。
          如果照搬长版 prompt，模型根本看不到 schema 末尾，业务规则也会被截断。

        精简策略：
          1) System prompt 写死成一句"只生成单条只读 SQL"（在 sql_generator.py 里）
          2) Schema 段用 _format_compact_metadata()，只输出 "表名(字段 类型, ...)"，不写注释
          3) Rules 段用英文短句，凑成 markdown 列表
          4) 不放 few-shot（业务规则 + 任务上下文已经够强引导了）

        三道安全闸门（执行流程）：
          1) _validate_controlled_budget()     上下文总预算不能爆
          2) 预算内有富余 → 直接返回完整 prompt
          3) 预算不够     → 砍每张表的字段数到 24；再不够就抛错（宁可报错也不让模型猜）

        context 字段（来自 backend/agent/contracts.py:XiYanPromptContext）：
          - source_id / dialect / schema_signature
          - question / task_goal / required_object_ids / planned_output_fields
          - schema_closure_object_ids / allowed_field_ids / max_rows
        """
        # 第 1 关：先校验总预算（输入+输出+safety_margin）不能超过本地模型上下文窗口
        # 防止"prompt 拼出来了，但生成时一定爆 OOM"
        self._validate_controlled_budget()

        # With the 6200-token input budget, reuse the Chat-quality metadata:
        # table summaries, column comments and foreign-key hints for the full
        # retrieve-stage schema closure.  The plan objects remain an execution
        # contract below; keeping their join neighbours here lets XiYan choose
        # the documented relationship rather than inventing one.
        schema_objects = context.schema_closure_object_ids or context.required_object_ids
        annotated_schema = self._format_metadata(metadata, schema_objects)
        source_profile = self.get_agent_source_template(context.source_id)
        source_examples = self._agent_source_examples(context.source_id, context.question)
        catalog_hint = catalog_prompt_hint(
            context.source_id,
            [table.get("name", "") for table in metadata.get("tables", [])],
        )

        # 第 3 步：拼装"硬约束"列表 —— 这些是 Agent 链路上游已经审过的"金科玉律"
        rules = [
            "Only output one SELECT or WITH SQL statement, with no explanation or Markdown.",
            "Use only tables and fields shown in Schema. Never invent fields or access another source.",
            f"Use {context.dialect} syntax and add a LIMIT no greater than {context.max_rows} when the query can return detail rows.",
        ]
        # 以下三行是"plan 阶段审过的"——把 plan 摘要回灌进 prompt，等于把"批准过的事"硬塞给模型
        if context.task_goal:
            rules.append(f"Approved task goal (must be implemented without narrowing it): {context.task_goal}")
        if context.required_object_ids:
            rules.append(f"Approved task objects: {', '.join(context.required_object_ids)}. Use every object required to implement the goal's filters and joins.")
        if context.planned_output_fields:
            rules.append(f"Planned output fields: {', '.join(context.planned_output_fields)}. Do not replace a detail-record request with an unrelated aggregate.")
        # 全局规则：中文"YYYY年M月"必须用半开区间 [月初, 下月初)
        rules.append("For every Chinese YYYY年M月 constraint, use an explicit half-open range from YYYY-MM-01 00:00:00 (inclusive) to the following month YYYY-MM-01 00:00:00 (exclusive).")
        # 数据源专属业务规则（半开日期、最新价视图、地址别名 LIKE…）
        # 这份规则同时在 chat 路径（build_xiyan_prompt）和 agent 规划（QwenPlanner）里被引用
        rules.extend(source_profile["sql_rules"])
        # These are the detailed, source-specific semantic constraints already
        # proven in the Chat template.  They complement (rather than replace)
        # the short universal rules above.
        rules.extend(self._agent_reference_rules(context.source_id, context.question))

        # Keep the same source-specific examples used by Chat.  The model must
        # adapt them only to the approved schema above.
        def render(schema_text: str, *, include_catalog: bool, include_examples: bool) -> str:
            parts = [
            "You are a controlled Text-to-SQL generator.",
            f"Source: {context.source_id}; dialect: {context.dialect}",
            f"Question: {context.question}",
            "Shared SQL principles (same base template as Chat):",
            self.system_prompt,
            "Final selected schema (all listed tables, fields, comments, and foreign keys are authoritative):",
            schema_text,
            "SQL:\n```sql",   # 末尾留个开口，模型接着写 SQL
        ] + (
            ["Catalog and business synonym reference:", catalog_hint]
            if include_catalog and catalog_hint else []
        ) + [
            "Approved execution contract:",
            *[f"- {rule}" for rule in rules],
        ] + (
            ["Source-specific reference example (adapt only to the approved Schema):", source_examples]
            if include_examples and source_examples else []
        ) + ["SQL:\n```sql"]
            parts.remove("SQL:\n```sql")
            return "\n".join(parts)

        prompt = render(annotated_schema, include_catalog=True, include_examples=True)

        # 第 5 关：预算足够就直接返回（绝大多数情况走这里）
        if self._estimate_xiyan_tokens(prompt) <= settings.agent_xiyan_prompt_token_budget:
            return prompt

        # 第 6 关（兜底）：超出预算。保留问题/方言/规则，只压缩 schema 的字段数
        # 优先级：用户问题 + 方言 + 业务规则 > schema 字段
        # 这里用 max_fields_per_object=24 截断多余的列，加 "…" 标记
        prompt = render(annotated_schema, include_catalog=False, include_examples=False)
        # 压缩后还超就抛错——不静默提交截断的 prompt 给模型
        # 这种情况通常是 schema_closure_object_ids 里塞了太多表，应该回去重新做表选择
        if self._estimate_xiyan_tokens(prompt) > settings.agent_xiyan_prompt_token_budget:
            raise ValueError("CONTROLLED_XIYAN_COMPLETE_SCHEMA_BUDGET_EXCEEDED")
        return prompt

    def _agent_reference_rules(self, data_source: str, question: str) -> list[str]:
        """Select only the source rules relevant to the current intent.

        Chat can afford a broad knowledge block.  The local XiYan generator
        should instead receive the common source contract plus the one rule
        group that matches the user's intent; this avoids unrelated address,
        people, and residence rules competing for attention.
        """
        question_lower = question.lower()
        if data_source not in {"mysql_police_address", "police_address"}:
            return []

        rules: list[str] = []
        if any(term in question_lower for term in ("地址别名", "别名")):
            rules.extend([
                "Address aliases must join addr_alias aa to addr_standard_address sa ON aa.std_address_code = sa.std_address_code; return aa.alias_name and sa.full_address. Never use nonexistent aa.std_address_id.",
                "For an address-alias question containing a literal name X, filter aa.alias_name LIKE '%X%'; extract the user's actual name rather than searching a placeholder.",
            ])
        if any(term in question_lower for term in ("当前居住", "登记居住", "居住人员", "出租房", "小区")):
            rules.extend([
                "For current registered residents use v_nl2sql_person_current_address. For rental houses and current resident counts use v_nl2sql_house_occupancy. These current-state views do not expose is_current, so never add is_current = 1 to them.",
                "For a residential-compound name use full_address LIKE '%小区名%'. residential_code is a code, and v_nl2sql_house_occupancy has no residential_name column.",
            ])
        if any(term in question_lower for term in ("嫌疑", "受害", "证人", "亲属", "涉及人员", "涉案人员")):
            rules.append(
                "For involved people, suspects, victims, witnesses, or relatives, join alert_involvement ai ON pa.alert_no = ai.alert_no. Use ai.name and ai.role_code; role codes are SUSPECT, VICTIM, WITNESS, RELATIVE, and INVOLVED."
            )
        if "未关联案事件" in question_lower or "没有案事件" in question_lower:
            rules.append(
                "For closed alerts without an event, use LEFT JOIN alert_event e ON e.alert_no = a.alert_no with a.alert_status_code = 'CLOSED' AND e.alert_no IS NULL."
            )
        return rules

    def _agent_source_examples(self, data_source: str, question: str) -> str:
        """Return one intent-matched few-shot, not every example of a source."""
        question_lower = question.lower()
        if data_source in {"mysql_police_address", "police_address"}:
            if any(term in question_lower for term in ("地址别名", "别名")):
                return """## 警务示例：地址别名查询
```sql
SELECT aa.alias_name AS 地址别名, sa.full_address AS 标准地址
FROM addr_alias aa
JOIN addr_standard_address sa ON aa.std_address_code = sa.std_address_code
WHERE aa.alias_name LIKE '%目标词%'
LIMIT 100;
```"""
            if any(term in question_lower for term in ("当前居住", "登记居住", "居住人员", "小区")):
                return """## 警务示例：小区当前登记居住人员统计
```sql
SELECT COUNT(DISTINCT person_code) AS 当前登记人数
FROM v_nl2sql_person_current_address
WHERE full_address LIKE '%目标小区%';
```"""
            if any(term in question_lower for term in ("报警内容", "警情内容", "提到", "关键词")):
                return """## 警务示例：报警内容关键词统计
```sql
SELECT COUNT(DISTINCT alert_no) AS 警情数量
FROM v_nl2sql_alert_detail
WHERE alert_content LIKE '%目标词%';
```"""
            return """## 警务示例：按条件统计警情数量
```sql
SELECT COUNT(DISTINCT alert_no) AS 警情数量
FROM v_nl2sql_alert_detail
WHERE alert_time >= '起始时间'
  AND alert_time < '结束时间';
```"""
        return self._few_shots_for(data_source)

    def get_agent_source_template(self, data_source: str) -> Dict[str, Any]:
        """【项目里最关键的"共享规则表"】返回数据源专属 profile，被三处共用：

          1) build_xiyan_prompt()             — chat 路径把 sql_rules 写到 prompt 末尾
          2) build_controlled_xiyan_prompt()  — agent 路径同样把 sql_rules 写进 prompt
          3) QwenPlanner / QwenPlanReviewer   — agent 规划/审核阶段把它当作只读 planning_tool 喂给 LLM

        这种"一处维护、三处共享"的设计，避免 chat 和 agent 两条路径在业务规则上"语义漂移"——
        比如某天把"半开日期区间"从规则里删了，chat 和 agent 行为会同时改变，不会出现一个对、一个错。

        字段说明（每个 profile）：
          - template_id   : 给运维/审计看的人类可读 ID
          - planning_hint : 规划阶段用的简短提示（"Hadoop 由本地 SQLite 执行 CSV 表"等）
          - sql_rules     : 必带的 SQL 业务规则数组（半开日期、最新价视图、地址别名 LIKE…）

        加新数据源时：在这里加一个 profile 即可，三处调用方都会自动生效。
        """
        profiles: Dict[str, Dict[str, Any]] = {
            "hive_hadoop_demo": {
                "template_id": "hadoop_sqlite_csv",
                "planning_hint": "Hadoop demo is executed as local SQLite CSV tables; monthly dates are TEXT YYYY-MM-DD.",
                "sql_rules": [
                    "For monthly Hadoop statistics use substr(event_date, 1, 7); never use TO_DATE, DATE_FORMAT or date_trunc.",
                    "Brand trends require hadoop_order_events joined to hadoop_product_dim by product_id.",
                ],
            },
            "postgres_stock": {
                "template_id": "postgres_stock_market",
                "planning_hint": "Keep latest-price views separate from historical price detail when a historical date range is requested.",
                "sql_rules": [
                    "v_stock_latest_price has no sector_code; join stock_symbols on symbol and exchange_code for sector filters.",
                    "Historical comparisons use v_stock_price_detail with a half-open date range.",
                ],
            },
            "sqlite_demo": {
                "template_id": "sqlite_business_demo",
                "planning_hint": "Use indexed half-open date ranges and explicit employee/sales/department joins.",
                "sql_rules": [
                    "Use half-open date ranges instead of YEAR or STRFTIME for year filters.",
                    "Department sales join sales -> employees -> departments; use LEFT JOIN from departments only when zero-sales departments are requested.",
                ],
            },
            "mysql_police_address": {
                "template_id": "mysql_police_address",
                "planning_hint": "Use alert-detail views for police aggregates; named involved people require the involvement relation and role code.",
                "sql_rules": [
                    "For a Chinese calendar month use alert_time >= first day and < following month's first day; never use the next day as upper bound.",
                    "Do not add alert type or status filters unless the approved task goal explicitly asks for them.",
                    "Named involved people require JOIN alert_involvement i ON i.alert_no = a.alert_no; suspects require i.role_code = 'SUSPECT'.",
                    "police_alert uses alert_status; alert_status_code belongs to the alert-detail view. Join dict_alert_role by role_code, never role_id.",
                    "警务报警统计优先使用 v_nl2sql_alert_detail；治安报警使用 alert_type_code = 'SECURITY'；已结案使用 alert_status_code = 'CLOSED'；区域使用 district_name。",
                    "统计警情数量使用 COUNT(DISTINCT alert_no)。当前居住人员使用 v_nl2sql_person_current_address，小区名称使用 full_address LIKE。",
                ],
            },
            "police_address": {
                "template_id": "mysql_police_address",
                "planning_hint": "Use alert-detail views for police aggregates; named involved people require the involvement relation and role code.",
                "sql_rules": [],
            },
            "gauss_ecommerce": {
                "template_id": "gauss_ecommerce",
                "planning_hint": "All-city completed-order questions must retain cities with zero matching orders.",
                "sql_rules": [
                    "For all-city completed-order totals, start from customers and LEFT JOIN orders; put status and date filters in ON and use COALESCE(SUM(...), 0).",
                    "v_order_summary uses customer_city; customers uses city.",
                ],
            },
            "dameng_ecommerce": {
                "template_id": "dameng_ecommerce_oracle",
                "planning_hint": "Use Oracle-compatible uppercase objects and retain zero-order cities for all-city totals.",
                "sql_rules": [
                    "For all-city completed-order totals, start from CUSTOMERS and LEFT JOIN ORDERS; put status and date filters in ON and use COALESCE(SUM(...), 0).",
                    "V_ORDER_SUMMARY uses CUSTOMER_CITY; CUSTOMERS uses CITY.",
                ],
            },
            "countries_graphql": {
                "template_id": "graphql_virtual_table",
                "planning_hint": "GraphQL data is a fixed backend response mapped to a read-only virtual table.",
                "sql_rules": ["Use only the retrieved virtual-table Schema; never construct GraphQL operations or URLs."],
            },
            "rest_api_demo": {
                "template_id": "rest_virtual_table",
                "planning_hint": "REST data is a fixed backend response mapped to a read-only virtual table.",
                "sql_rules": ["Use only the retrieved virtual-table Schema; never construct URLs, methods or request parameters."],
            },
        }
        profile = profiles.get(data_source, {
            "template_id": "generic_readonly_sql",
            "planning_hint": "Use only the retrieved Schema closure and source dialect.",
            "sql_rules": [],
        }).copy()
        if data_source == "police_address":
            profile["sql_rules"] = profiles["mysql_police_address"]["sql_rules"]
        return {"source_id": data_source, **profile}

    @staticmethod
    def _estimate_xiyan_tokens(text: str) -> int:
        """粗估 prompt 的 token 数。

        这是个保守估计：用 "每 3 字符 ≈ 1 token" 的近似公式。
        CJK 和 ASCII 的实际 token 化方式不同，这里故意把分母取大（3 而不是 4），
        留出余量防止"估算说够用，实际爆 OOM"。
        """
        # CJK characters and ASCII identifiers are tokenized differently.  The
        # 2.5-character estimate intentionally leaves room below the 4K limit.
        return max(1, (len(text) + 2) // 3)

    @staticmethod
    def _validate_controlled_budget() -> None:
        """校验"输入预算 + 输出预算 + 安全边际"的总和不能超过模型上下文窗口。

        在拼 prompt 之前先检查配置是否合理——
        如果配置本身就不可能（光 prompt+output 就超过 4K），直接抛错，提示运维去改 settings，
        而不是等到运行时才暴 OOM。
        """
        total = (
            settings.agent_xiyan_prompt_token_budget
            + settings.agent_xiyan_max_output_tokens
            + settings.agent_xiyan_safety_margin_tokens
        )
        if total > settings.agent_xiyan_context_window:
            raise ValueError("CONTROLLED_XIYAN_CONTEXT_BUDGET_INVALID")

    @staticmethod
    def _format_compact_metadata(
        metadata: Dict[str, Any],
        relevant_tables: List[str],
        max_fields_per_object: int | None = None,
    ) -> str:
        """【Agent 路径专用】把元数据压成 "表名(字段 类型, ...)" 单行格式。

        与 _format_metadata() 的差别：
          - 本函数：不写注释、不写外键、不写 CREATE TABLE 包装——纯字段清单
          - _format_metadata()：完整的 CREATE TABLE 风格（注释、外键关系都保留）

        触发"再压缩"：当 max_fields_per_object 给了上限时，超过上限的字段会被截掉，末尾加 "…"
        """
        relevant = set(relevant_tables)
        lines: list[str] = []
        for table in metadata.get("tables", []):
            name = str(table.get("name") or "")
            if not name or name not in relevant:
                continue
            fields = [
                f"{column.get('name', '')} {column.get('type', '')}".strip()
                for column in table.get("columns", [])
                if column.get("name")
            ]
            if max_fields_per_object and len(fields) > max_fields_per_object:
                fields = fields[:max_fields_per_object] + ["…"]
            lines.append(f"{name}({', '.join(fields)})")
        return "\n".join(lines) or "(no approved schema objects)"

    def _format_metadata(self, metadata: Dict[str, Any],
                         relevant_tables: List[str] = None,
                         max_fields_per_object: int | None = None) -> str:
        """【Chat 路径专用】把元数据格式化成完整的 CREATE TABLE 风格文本。

        为什么用 CREATE TABLE 风格？
          LLM 在训练时见过大量 CREATE TABLE 语句，用这种格式喂给它，它能最快最准确地理解：
            - 有哪些表
            - 每张表有哪些字段、什么类型
            - 表之间的外键关系
          效果比直接传 JSON 好得多（这是 NL2SQL 领域的工程经验）。

        输入 metadata 结构（参考 contracts.MetadataContext.tables 字段）：
            {
              "tables": [
                {
                  "name": "employees",
                  "comment": "员工信息表",
                  "summary": "...",        # 可选，比 comment 更精炼
                  "columns": [
                    {"name": "id", "type": "INTEGER", "comment": "主键"},
                    {"name": "name", "type": "TEXT", "comment": ""}
                  ],
                  "foreign_keys": [
                    {"column": "department_id", "ref_table": "departments", "ref_column": "id"}
                  ]
                },
                ...
              ]
            }

        输出示例：
            ### 表: employees (员工信息表)
            CREATE TABLE employees (
                id INTEGER -- 主键,
                name TEXT,
                -- 外键关系:
                --   department_id -> departments(id)
            );
        """

        # 提取表名
        tables = metadata.get("tables", [])
        # 遍历，只保存相关表（relevant_tables 是上游表选择器筛过的白名单）
        if relevant_tables:
            tables = [t for t in tables if t["name"] in relevant_tables]

        lines = []
        for table in tables:
            # summary 是元数据汇总器（metadata_summarizer.py）生成的精炼注释；
            # 老数据源只有 comment 字段，所以两者取其一
            table_comment = table.get("summary") or table.get("comment", "")
            lines.append(f"### 表: {table['name']} ({table_comment})")
            lines.append("CREATE TABLE " + table['name'] + " (")
            # 遍历每一列的 name, type, comment
            columns = table.get("columns", [])
            if max_fields_per_object and len(columns) > max_fields_per_object:
                columns = columns[:max_fields_per_object]
            for col in columns:
                comment = f" -- {col.get('comment', '')}" if col.get('comment') else ""
                lines.append(f"    {col['name']} {col['type']}{comment},")
            if max_fields_per_object and len(table.get("columns", [])) > max_fields_per_object:
                lines.append("    -- ... remaining fields omitted only to fit the controlled prompt budget,")

            if table.get("foreign_keys"):
                lines.append("    -- 外键关系:")
                # 遍历每一个外键关系
                for fk in table["foreign_keys"]:
                    lines.append(f"    --   {fk['column']} -> {fk['ref_table']}({fk['ref_column']})")
            lines.append(");")
            lines.append("")

        # 兜底：万一没拼出任何内容，给模型一个明确提示，避免它瞎猜表名
        if not lines:
            return "（无可用元数据，请检查数据源连接）"

        return "\n".join(lines)
