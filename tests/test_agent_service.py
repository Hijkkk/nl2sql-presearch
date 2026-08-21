from backend.agent.contracts import MetadataContext, SourceCandidate, SourceDescriptor
from backend.agent.service import AgentPreparationService


def test_prepare_skips_an_unavailable_candidate_without_blocking_sqlite(monkeypatch):
    sqlite = SourceCandidate(
        source_id="sqlite_demo", source_type="sqlite", dialect="sqlite", description="员工部门", score=10.0
    )
    dameng = SourceCandidate(
        source_id="dameng_ecommerce", source_type="dameng", dialect="oracle", description="电商", score=2.0
    )
    sqlite_context = MetadataContext(
        source=SourceDescriptor(source_id="sqlite_demo", source_type="sqlite", dialect="sqlite", description="员工部门"),
        selected_object_ids=["departments"],
        schema_closure_object_ids=["departments"],
        tables=[{"name": "departments", "columns": [{"name": "id"}]}],
    )

    monkeypatch.setattr("backend.agent.service.discover_sources", lambda *_args, **_kwargs: [sqlite, dameng])

    def retrieve(_question, candidate, **_kwargs):
        if candidate.source_id == "dameng_ecommerce":
            raise RuntimeError("Dameng JVM unavailable")
        return sqlite_context

    monkeypatch.setattr("backend.agent.service.retrieve_metadata_context", retrieve)

    candidates, contexts = AgentPreparationService().prepare("统计每个部门分别有多少名员工")

    assert [item.source_id for item in candidates] == ["sqlite_demo"]
    assert [item.source.source_id for item in contexts] == ["sqlite_demo"]
