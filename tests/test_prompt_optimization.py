import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.nl2sql.prompt_builder import PromptBuilder
from backend.nl2sql.sql_generator import SQLGenerator


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
        def build_prompt(self, question, metadata, relevant_tables=None):
            recorded["question"] = question
            recorded["relevant_tables"] = relevant_tables
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

    sql, thought, error = asyncio.run(
        generator.generate_sql(
            "统计每个部门的销售总额",
            build_sample_metadata(),
            dialect="sqlite",
        )
    )

    assert error is None
    assert sql == "SELECT 1;"
    assert "部门" in thought
    assert recorded["relevant_tables"] == ["departments", "sales"]
