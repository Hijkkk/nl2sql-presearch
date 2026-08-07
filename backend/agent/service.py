"""Preparation service for the controlled-agent path.

The first rollout is intentionally record-only: it proves source routing and
schema retrieval before any model-generated plan or SQL can change execution.
"""
from __future__ import annotations

from backend.agent.contracts import MetadataContext, SourceCandidate
from backend.agent.tools import discover_sources, retrieve_metadata_context
from backend.config.config import settings


class AgentPreparationService:
    def prepare(self, question: str) -> tuple[list[SourceCandidate], list[MetadataContext]]:
        """Return bounded candidates and metadata contexts for planner evaluation."""
        candidates = discover_sources(question, limit=settings.agent_source_candidate_limit)
        contexts: list[MetadataContext] = []
        for candidate in candidates:
            # A zero-score candidate is not evidence for automatic routing.
            # It remains visible to an operator, but does not trigger metadata I/O.
            if candidate.score <= 0:
                continue
            contexts.append(
                retrieve_metadata_context(question, candidate, object_limit=settings.agent_schema_object_limit)
            )
        return candidates, contexts
