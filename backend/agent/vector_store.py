"""Qdrant-backed semantic retrieval for public NL2SQL metadata only.

The collection deliberately contains source descriptions, schema comments and
Catalog hints.  It never stores connection configuration, credentials, or data
rows.  Semantic retrieval improves recall; policy validation and schema closure
remain the authority for what the model may use.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable

import httpx
from qdrant_client import QdrantClient, models

from backend.agent.contracts import SourceDescriptor
from backend.config.config import settings
from backend.nl2sql.catalog import catalog_prompt_hint


@dataclass(frozen=True)
class MetadataDocument:
    document_id: str
    text: str
    payload: dict[str, Any]


class DashScopeEmbeddingClient:
    """Small OpenAI-compatible embedding client; errors contain no secrets."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not settings.dashscope_api_key:
            raise RuntimeError("DashScope embedding is not configured")
        # qwen3.7-text-embedding accepts at most 20 texts per request.  Chunking
        # makes a full metadata rebuild work for large schemas without changing
        # the query-time one-text request.
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 20):
            batch = texts[start:start + 20]
            response = httpx.post(
                f"{settings.dashscope_base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
                json={
                    "model": settings.agent_embedding_model,
                    "input": batch,
                    "dimensions": settings.agent_embedding_dimensions,
                    "encoding_format": "float",
                },
                timeout=settings.agent_embedding_timeout,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list):
                raise RuntimeError("DashScope embedding returned an invalid response")
            ordered = sorted((item for item in data if isinstance(item, dict)), key=lambda item: int(item.get("index", -1)))
            batch_vectors = [item.get("embedding") for item in ordered]
            if len(batch_vectors) != len(batch) or any(not isinstance(vector, list) for vector in batch_vectors):
                raise RuntimeError("DashScope embedding response is incomplete")
            vectors.extend(batch_vectors)
        return vectors


class QdrantMetadataStore:
    def __init__(self) -> None:
        self.client = QdrantClient(url=settings.agent_qdrant_url, timeout=settings.agent_embedding_timeout)
        self.embedding = DashScopeEmbeddingClient()

    def ready(self) -> bool:
        try:
            return self.client.collection_exists(settings.agent_qdrant_collection)
        except Exception:
            return False

    def rebuild(self, documents: list[MetadataDocument]) -> int:
        if not documents:
            return 0
        self.client.recreate_collection(
            collection_name=settings.agent_qdrant_collection,
            vectors_config=models.VectorParams(
                size=settings.agent_embedding_dimensions,
                distance=models.Distance.COSINE,
            ),
        )
        vectors = self.embedding.embed([document.text for document in documents])
        points = [
            models.PointStruct(id=document.document_id, vector=vector, payload=document.payload)
            for document, vector in zip(documents, vectors, strict=True)
        ]
        self.client.upsert(settings.agent_qdrant_collection, points=points, wait=True)
        return len(points)

    def replace_sources(self, documents: list[MetadataDocument], source_ids: list[str]) -> int:
        """Atomically replace only successfully fetched source metadata.

        Unlike a collection rebuild, a temporarily unavailable source keeps its
        prior points.  This is the safe path for incremental rollout.
        """
        if not documents or not source_ids:
            return 0
        if not self.ready():
            self.client.create_collection(
                collection_name=settings.agent_qdrant_collection,
                vectors_config=models.VectorParams(
                    size=settings.agent_embedding_dimensions,
                    distance=models.Distance.COSINE,
                ),
            )
        source_filter = models.Filter(
            must=[models.FieldCondition(key="source_id", match=models.MatchAny(any=source_ids))]
        )
        self.client.delete(settings.agent_qdrant_collection, points_selector=source_filter, wait=True)
        vectors = self.embedding.embed([document.text for document in documents])
        points = [
            models.PointStruct(id=document.document_id, vector=vector, payload=document.payload)
            for document, vector in zip(documents, vectors, strict=True)
        ]
        self.client.upsert(settings.agent_qdrant_collection, points=points, wait=True)
        return len(points)

    def search(self, question: str, *, kind: str, source_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        if not self.ready() or limit <= 0:
            return []
        must = [models.FieldCondition(key="kind", match=models.MatchValue(value=kind))]
        if source_id:
            must.append(models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id)))
        response = self.client.query_points(
            settings.agent_qdrant_collection,
            query=self.embedding.embed([question])[0],
            query_filter=models.Filter(must=must),
            limit=limit,
            with_payload=True,
        )
        return [
            {**(point.payload or {}), "vector_score": float(point.score)}
            for point in response.points
        ]


def _document_id(*parts: str) -> str:
    # Qdrant accepts UUID ids; a deterministic UUID-like hash makes rebuilds idempotent.
    digest = sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def build_source_document(source: SourceDescriptor) -> MetadataDocument:
    text = "\n".join((
        f"数据源: {source.source_id}",
        f"类型: {source.source_type}; 方言: {source.dialect}",
        f"介绍: {source.description}",
        f"能力: {', '.join(source.capabilities)}",
    ))
    return MetadataDocument(_document_id("source", source.source_id), text, {
        "kind": "source", "source_id": source.source_id, "source_type": source.source_type,
        "dialect": source.dialect,
    })


def build_metadata_documents(source: SourceDescriptor, metadata: dict[str, Any]) -> list[MetadataDocument]:
    documents = [build_source_document(source)]
    for table in metadata.get("tables", []):
        name = str(table.get("name") or "")
        if not name:
            continue
        columns = "; ".join(
            f"{column.get('name', '')} {column.get('comment', '')}".strip()
            for column in table.get("columns", [])
        )
        text = "\n".join((
            f"数据源: {source.source_id}; 方言: {source.dialect}",
            f"对象: {name}",
            f"说明: {table.get('comment') or table.get('summary') or ''}",
            f"字段: {columns}",
        ))
        documents.append(MetadataDocument(_document_id("object", source.source_id, name), text, {
            "kind": "object", "source_id": source.source_id, "object_id": name,
        }))
    hint = catalog_prompt_hint(source.source_id, [str(table.get("name")) for table in metadata.get("tables", [])])
    if hint:
        documents.append(MetadataDocument(_document_id("catalog", source.source_id), hint, {
            "kind": "catalog", "source_id": source.source_id,
        }))
    return documents


def create_metadata_store() -> QdrantMetadataStore | None:
    if not settings.agent_vector_enabled:
        return None
    return QdrantMetadataStore()
