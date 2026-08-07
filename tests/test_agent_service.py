from backend.agent.contracts import SourceCandidate
from backend.agent.service import AgentPreparationService


def test_agent_preparation_does_not_read_metadata_for_zero_score_candidates(monkeypatch):
    candidates = [
        SourceCandidate(source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警情", score=4),
        SourceCandidate(source_id="postgres_stock", source_type="postgresql", dialect="postgres", description="股票", score=0),
    ]
    read_sources = []

    monkeypatch.setattr("backend.agent.service.discover_sources", lambda question, limit: candidates)
    monkeypatch.setattr(
        "backend.agent.service.retrieve_metadata_context",
        lambda question, candidate, object_limit: read_sources.append((candidate.source_id, object_limit)) or {"source": candidate.source_id},
    )

    result_candidates, contexts = AgentPreparationService().prepare("统计警情")

    assert result_candidates == candidates
    assert contexts == [{"source": "mysql_police_address"}]
    assert read_sources == [("mysql_police_address", 5)]
