import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.nl2sql.prompt_builder import PromptBuilder
from backend.nl2sql.sql_generator import SQLGenerator
from backend.config.config import settings
from backend.agent.contracts import XiYanPromptContext


def build_sample_metadata():
    return {
        "total_tables": 3,
        "tables": [
            {
                "name": "employees",
                "comment": "员工信息表",
                "summary": "保存员工基础信息、薪资和所属部门。",
                "columns": [
                    {"name": "id", "type": "INTEGER", "comment": "主键"},
                    {"name": "department_id", "type": "INTEGER", "comment": "部门ID"},
                    {"name": "salary", "type": "REAL", "comment": "薪资"},
                ],
                "foreign_keys": [
                    {"column": "department_id", "ref_table": "departments", "ref_column": "id"}
                ],
            },
            {
                "name": "departments",
                "comment": "部门信息表",
                "summary": "保存部门名称、地点和预算。",
                "columns": [
                    {"name": "id", "type": "INTEGER", "comment": "主键"},
                    {"name": "name", "type": "TEXT", "comment": "部门名称"},
                ],
                "foreign_keys": [],
            },
            {
                "name": "sales",
                "comment": "销售记录表",
                "summary": "保存员工销售金额和产品分类。",
                "columns": [
                    {"name": "id", "type": "INTEGER", "comment": "主键"},
                    {"name": "employee_id", "type": "INTEGER", "comment": "员工ID"},
                    {"name": "amount", "type": "REAL", "comment": "销售额"},
                ],
                "foreign_keys": [
                    {"column": "employee_id", "ref_table": "employees", "ref_column": "id"}
                ],
            },
        ],
    }


def test_prompt_builder_prefers_table_summary():
    builder = PromptBuilder()

    schema_text = builder._format_metadata(
        build_sample_metadata(),
        relevant_tables=["employees"],
    )

    assert "保存员工基础信息、薪资和所属部门" in schema_text
    assert "员工信息表" not in schema_text
    assert "CREATE TABLE employees" in schema_text
    assert "department_id -> departments(id)" in schema_text


def test_sql_generator_passes_selected_tables_to_prompt_builder():
    generator = SQLGenerator()
    recorded = {}

    class RecordingPromptBuilder:
        def build_prompt(self, question, metadata, relevant_tables=None, data_source=""):
            recorded["question"] = question
            recorded["relevant_tables"] = relevant_tables
            recorded["data_source"] = data_source
            return "prompt"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "思考：需要按部门统计销售额。\n```sql\nSELECT 1;\n```"
                        }
                    }
                ]
            }

    class FakeClient:
        def post(self, *args, **kwargs):
            return FakeResponse()

    generator.prompt_builder = RecordingPromptBuilder()
    generator.client = FakeClient()

    sql, thought, error, trace = asyncio.run(
        generator.generate_sql(
            "统计每个部门的销售总额",
            build_sample_metadata(),
            dialect="sqlite",
        )
    )

    assert error is None
    assert sql == "SELECT 1;"
    assert "部门" in thought
    assert trace["raw_model_output"]
    assert set(recorded["relevant_tables"]) == {"employees", "departments", "sales"}


def test_prompt_builder_requires_chinese_aliases():
    prompt = PromptBuilder().build_prompt(
        "查询 AAA 的交易日期、开盘价、收盘价和成交量",
        {
            "total_tables": 1,
            "tables": [
                {
                    "name": "stock_daily_prices",
                    "comment": "股票日行情表",
                    "columns": [
                        {"name": "symbol", "type": "varchar", "comment": "股票代码"},
                        {"name": "trade_date", "type": "date", "comment": "交易日期"},
                        {"name": "open_price", "type": "numeric", "comment": "开盘价"},
                        {"name": "close_price", "type": "numeric", "comment": "收盘价"},
                        {"name": "volume", "type": "int8", "comment": "成交量"},
                    ],
                    "foreign_keys": [],
                }
            ],
        },
    )

    assert "trade_date AS 交易日期" in prompt
    assert "open_price AS 开盘价" in prompt
    assert "不要臆造元数据中不存在的字段" in prompt


def build_hadoop_metadata():
    return {
        "total_tables": 4,
        "tables": [
            {
                "name": "hadoop_order_events",
                "comment": "HDFS 上的订单事件事实表，含事件日期、用户、商品、地域、订单数量和成交金额 GMV。",
                "columns": [
                    {"name": "event_date", "type": "TEXT", "comment": "事件日期，YYYY-MM-DD"},
                    {"name": "user_id", "type": "INTEGER", "comment": "下单用户 ID"},
                    {"name": "product_id", "type": "TEXT", "comment": "商品 ID"},
                    {"name": "region_id", "type": "TEXT", "comment": "地域 ID"},
                    {"name": "order_count", "type": "INTEGER", "comment": "订单数量，建议别名为 销量 或 订单数"},
                    {"name": "gmv", "type": "REAL", "comment": "成交金额，建议别名为 成交金额"},
                ],
                "foreign_keys": [
                    {"column": "user_id", "ref_table": "hadoop_user_profiles", "ref_column": "user_id"},
                    {"column": "product_id", "ref_table": "hadoop_product_dim", "ref_column": "product_id"},
                    {"column": "region_id", "ref_table": "hadoop_region_dim", "ref_column": "region_id"},
                ],
            },
            {
                "name": "hadoop_user_profiles",
                "comment": "HDFS 上的用户维度表，含用户姓名、VIP 等级、所在地域。",
                "columns": [
                    {"name": "user_id", "type": "INTEGER", "comment": "用户 ID"},
                    {"name": "user_name", "type": "TEXT", "comment": "用户姓名"},
                    {"name": "vip_level", "type": "INTEGER", "comment": "VIP 等级"},
                    {"name": "region_id", "type": "TEXT", "comment": "用户所在地域 ID"},
                ],
                "foreign_keys": [{"column": "region_id", "ref_table": "hadoop_region_dim", "ref_column": "region_id"}],
            },
            {
                "name": "hadoop_product_dim",
                "comment": "HDFS 上的商品维度表，含商品名称、分类和品牌。",
                "columns": [
                    {"name": "product_id", "type": "TEXT", "comment": "商品 ID"},
                    {"name": "product_name", "type": "TEXT", "comment": "商品名称"},
                    {"name": "brand", "type": "TEXT", "comment": "品牌"},
                ],
                "foreign_keys": [],
            },
            {
                "name": "hadoop_region_dim",
                "comment": "HDFS 上的地域维度表，含省份、城市、城市分级和大区。",
                "columns": [
                    {"name": "region_id", "type": "TEXT", "comment": "地域 ID"},
                    {"name": "province", "type": "TEXT", "comment": "省份"},
                    {"name": "city", "type": "TEXT", "comment": "城市"},
                    {"name": "region_group", "type": "TEXT", "comment": "大区"},
                ],
                "foreign_keys": [],
            },
        ],
    }


def test_prompt_builder_guides_hadoop_star_schema_without_sales_fact():
    prompt = PromptBuilder().build_prompt(
        "各品牌每月销量趋势是什么？",
        build_hadoop_metadata(),
        data_source="hive_hadoop_demo",
    )

    assert "hadoop_order_events" in prompt
    assert "hadoop_product_dim" in prompt
    assert "hadoop_region_dim" in prompt
    assert "SUM(e.order_count) AS 销量" in prompt
    assert "hadoop_order_events e" in prompt


def test_prompt_builder_creates_xiyan_prompt_without_cot():
    prompt = PromptBuilder().build_xiyan_prompt(
        "统计每个洲分别有多少个国家。",
        {
            "total_tables": 1,
            "tables": [
                {
                    "name": "countries",
                    "comment": "国家数据",
                    "columns": [
                        {"name": "continent_name", "type": "TEXT", "comment": "所属洲"},
                        {"name": "name", "type": "TEXT", "comment": "国家名称"},
                    ],
                    "foreign_keys": [],
                }
            ],
        },
        dialect="SQLite",
    )

    assert "XiYan" not in prompt
    assert "请一步步思考" not in prompt
    assert "【数据库schema】" in prompt
    assert prompt.rstrip().endswith("```sql")


def test_prompt_builder_renders_existing_xiyan_rules_from_controlled_context():
    metadata = {
        "total_tables": 1,
        "tables": [{
            "name": "v_nl2sql_alert_detail",
            "comment": "警情明细视图",
            "columns": [{"name": "alert_no", "type": "TEXT", "comment": "警情编号"}],
            "foreign_keys": [],
        }],
    }
    context = XiYanPromptContext(
        source_id="mysql_police_address",
        dialect="MySQL",
        schema_signature="test-schema-v1",
        question="统计本月警情数量",
        schema_closure_object_ids=["v_nl2sql_alert_detail"],
        allowed_field_ids=["v_nl2sql_alert_detail.alert_no"],
        max_rows=1000,
    )

    prompt = PromptBuilder().build_controlled_xiyan_prompt(context, metadata)

    assert "Source: mysql_police_address; dialect: MySQL" in prompt
    assert "v_nl2sql_alert_detail(alert_no TEXT)" in prompt
    assert "警务报警统计优先使用 v_nl2sql_alert_detail" in prompt
    assert "COUNT(DISTINCT alert_no)" in prompt
    assert prompt.rstrip().endswith("```sql")


def test_controlled_xiyan_prompt_includes_approved_task_contract():
    metadata = {"tables": [{"name": "police_alert", "columns": [{"name": "alert_time", "type": "DATETIME"}]}]}
    context = XiYanPromptContext(
        source_id="mysql_police_address", dialect="MySQL", question="query",
        task_goal="January alert detail", required_object_ids=["police_alert", "alert_involvement"],
        planned_output_fields=["police_alert.alert_no"], schema_closure_object_ids=["police_alert"],
    )

    prompt = PromptBuilder().build_controlled_xiyan_prompt(context, metadata)

    assert "Approved task goal" in prompt
    assert "Approved task objects: police_alert, alert_involvement" in prompt
    assert "Do not replace a detail-record request with an unrelated aggregate." in prompt


def test_sql_generator_routes_xiyan_3b_to_local_openai_endpoint():
    generator = SQLGenerator()
    recorded = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "SELECT COUNT(*) AS 数量 FROM countries;"}}]}

    class FakeClient:
        def post(self, url, json, headers):
            recorded["url"] = url
            recorded["json"] = json
            recorded["headers"] = headers
            return FakeResponse()

    generator.client = FakeClient()
    sql, thought, error, trace = asyncio.run(
        generator.generate_sql(
            "统计国家数量",
            {
                "total_tables": 1,
                "tables": [
                    {
                        "name": "countries",
                        "comment": "国家数据",
                        "columns": [{"name": "name", "type": "TEXT", "comment": "国家名称"}],
                        "foreign_keys": [],
                    }
                ],
            },
            dialect="sqlite",
            model_id="xiyan-sql-qwencoder-3b",
        )
    )

    assert error is None
    assert sql == "SELECT COUNT(*) AS 数量 FROM countries;"
    assert trace["raw_model_output"] == "SELECT COUNT(*) AS 数量 FROM countries;"
    assert recorded["url"].endswith("/chat/completions")
    assert recorded["json"]["model"] == settings.xiyan_finetune_model
    assert recorded["json"]["temperature"] == 0.0
    assert recorded["json"]["max_tokens"] == 512


def test_sql_generator_routes_ollama_and_finetune_xiyan_separately():
    generator = SQLGenerator()

    ollama_base_url, ollama_model, ollama_api_key = generator._resolve_model_config(
        SQLGenerator.XIYAN_OLLAMA_MODEL_ID
    )
    finetune_base_url, finetune_model, finetune_api_key = generator._resolve_model_config(
        SQLGenerator.XIYAN_FINETUNE_MODEL_ID
    )
    default_base_url, default_model, _ = generator._resolve_model_config(
        SQLGenerator.DEFAULT_MODEL_ID
    )

    assert ollama_base_url == settings.xiyan_ollama_base_url
    assert ollama_model == settings.xiyan_ollama_model
    assert ollama_api_key == settings.xiyan_ollama_api_key
    assert finetune_base_url == settings.xiyan_finetune_base_url
    assert finetune_model == settings.xiyan_finetune_model
    assert default_base_url != ollama_base_url
    assert default_model == "Qwen3-Coder-Next-FP8"
