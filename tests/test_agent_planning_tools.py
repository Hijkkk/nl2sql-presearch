from backend.agent.contracts import MetadataContext, SourceCandidate, SourceDescriptor
from backend.agent.llm import _planning_tool_results
from backend.nl2sql.prompt_builder import PromptBuilder


def _police_context() -> MetadataContext:
    return MetadataContext(
        source=SourceDescriptor(
            source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警情"
        ),
        selected_object_ids=["v_nl2sql_alert_detail"],
        schema_closure_object_ids=["v_nl2sql_alert_detail"],
        tables=[{"name": "v_nl2sql_alert_detail", "columns": [{"name": "alert_no"}]}],
    )


def test_planning_tools_expose_only_retrieve_selected_context_and_template():
    candidate = SourceCandidate(
        source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警情", score=1
    )

    tools = _planning_tool_results([candidate], [_police_context()])

    assert [tool["name"] for tool in tools] == [
        "list_retrieved_sources", "get_retrieved_schema", "get_source_template_profile",
    ]
    assert tools[1]["result"][0]["source"]["source_id"] == "mysql_police_address"
    assert tools[1]["result"][0]["tables"][0]["name"] == "v_nl2sql_alert_detail"
    assert tools[2]["result"][0]["template_id"] == "mysql_police_address"
    assert "execute" not in " ".join(tool["name"] for tool in tools)


def test_shared_source_profile_is_used_by_controlled_prompt():
    profile = PromptBuilder().get_agent_source_template("postgres_stock")

    assert profile["template_id"] == "postgres_stock_market"
    assert any("sector_code" in rule for rule in profile["sql_rules"])
