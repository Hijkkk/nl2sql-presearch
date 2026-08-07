from backend.agent.contracts import AgentPlan, AgentSubtask, MetadataContext, SourceDescriptor
from backend.agent.policy import validate_plan


def _context(source_type: str) -> MetadataContext:
    return MetadataContext(
        source=SourceDescriptor(
            source_id=f"{source_type}_source",
            source_type=source_type,
            dialect="sqlite",
            description="固定端点映射的只读虚拟表",
        ),
        selected_object_ids=["virtual_view"],
        schema_closure_object_ids=["virtual_view"],
        tables=[{"name": "virtual_view", "columns": [{"name": "name"}]}],
    )


def test_virtual_api_sources_allow_only_readonly_sql_plan():
    for source_type in ("rest_api", "graphql"):
        context = _context(source_type)
        plan = AgentPlan(
            route_mode="single_source",
            subtasks=[
                AgentSubtask(
                    id="read_virtual_view",
                    source_id=context.source.source_id,
                    operation_id="readonly_sql",
                    goal="读取受控虚拟表",
                    object_ids=["virtual_view"],
                    output_fields=["virtual_view.name"],
                )
            ],
            confidence=1.0,
        )
        assert validate_plan(plan, [context]).status == "approved"
