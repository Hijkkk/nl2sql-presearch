"""Rebuild the local Qdrant metadata collection.

Run this intentionally as an operator action after schema changes, for example:
    python -m backend.agent.index_metadata --sources sqlite_demo
"""
from __future__ import annotations

import argparse

from backend.adapters.registry import get_adapter
from backend.agent.tools import configured_source_descriptors
from backend.agent.vector_store import build_metadata_documents, create_metadata_store


def main() -> int:
    parser = argparse.ArgumentParser(description="Index NL2SQL metadata into Qdrant")
    parser.add_argument("--sources", nargs="*", help="Source ids to fetch; default indexes every configured source")
    parser.add_argument("--dry-run", action="store_true", help="Fetch metadata and report coverage without writing Qdrant")
    parser.add_argument("--append", action="store_true", help="Replace only successfully fetched sources; preserve other collection points")
    args = parser.parse_args()
    descriptors = configured_source_descriptors()
    requested = set(args.sources or [source.source_id for source in descriptors])
    unknown = requested - {source.source_id for source in descriptors}
    if unknown:
        raise SystemExit(f"Unknown configured source(s): {', '.join(sorted(unknown))}")
    store = create_metadata_store()
    if not store:
        raise SystemExit("Set AGENT_VECTOR_ENABLED=true before indexing")

    # A source enters the semantic route only together with a successfully
    # fetched schema.  This prevents a descriptive but unindexed source from
    # winning routing and causing metadata I/O against an unavailable service.
    documents = []
    indexed_sources: list[str] = []
    failed_sources: list[str] = []
    for source in descriptors:
        if source.source_id not in requested:
            continue
        try:
            metadata = get_adapter(source.source_id).get_metadata()
            documents.extend(build_metadata_documents(source, metadata))
            indexed_sources.append(source.source_id)
        except Exception:
            # Do not publish a partial object's schema accidentally.  The source
            # description remains indexed and the runtime will use lexical fallback.
            failed_sources.append(source.source_id)

    if args.dry_run:
        print(f"dry_run_documents={len(documents)} ready_sources={','.join(indexed_sources)}")
    elif args.append:
        count = store.replace_sources(documents, indexed_sources)
        print(f"indexed_documents={count} indexed_sources={','.join(indexed_sources)} mode=append")
    else:
        count = store.rebuild(documents)
        print(f"indexed_documents={count} indexed_sources={','.join(indexed_sources)} mode=rebuild")
    if failed_sources:
        print(f"metadata_failed_sources={','.join(failed_sources)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
