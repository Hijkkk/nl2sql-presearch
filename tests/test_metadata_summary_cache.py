import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.nl2sql.metadata_summarizer import MetadataSummarizer


def sample_metadata():
    return {
        "total_tables": 1,
        "tables": [
            {
                "name": "orders",
                "comment": "订单信息表",
                "columns": [
                    {"name": "id", "type": "INTEGER", "comment": "主键"},
                    {"name": "customer_name", "type": "TEXT", "comment": "客户名称"},
                    {"name": "amount", "type": "REAL", "comment": "订单金额"},
                ],
                "primary_key": ["id"],
                "foreign_keys": [],
            }
        ],
    }


def test_summarizer_writes_fallback_cache(tmp_path):
    cache_path = tmp_path / "metadata_summaries.json"
    summarizer = MetadataSummarizer(cache_path=str(cache_path))

    summarized = asyncio.run(
        summarizer.summarize_metadata(
            sample_metadata(),
            data_source="demo",
            use_llm=False,
        )
    )

    assert cache_path.exists()
    assert summarized["tables"][0]["summary"].startswith("订单信息表")

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(cache) == 1
    assert next(iter(cache.values()))["generated_by"] == "fallback"


def test_summarizer_can_upgrade_fallback_cache_to_llm(tmp_path):
    cache_path = tmp_path / "metadata_summaries.json"
    summarizer = MetadataSummarizer(cache_path=str(cache_path))

    asyncio.run(
        summarizer.summarize_metadata(
            sample_metadata(),
            data_source="demo",
            use_llm=False,
        )
    )

    summarizer._call_llm = lambda prompt: "LLM 生成的订单业务摘要。"

    summarized = asyncio.run(
        summarizer.summarize_metadata(
            sample_metadata(),
            data_source="demo",
            use_llm=True,
        )
    )

    assert summarized["tables"][0]["summary"] == "LLM 生成的订单业务摘要。"

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert next(iter(cache.values()))["generated_by"] == "llm"


def test_summarizer_reuses_llm_cache_without_refresh(tmp_path):
    """A normal LLM-enabled request must not pay for the same summary twice."""
    summarizer = MetadataSummarizer(cache_path=str(tmp_path / "metadata_summaries.json"))
    summarizer._call_llm = lambda prompt: "第一次 LLM 摘要。"
    asyncio.run(summarizer.summarize_metadata(sample_metadata(), data_source="demo", use_llm=True))

    def should_not_call_llm(prompt):
        raise AssertionError("LLM cache should have been reused")

    summarizer._call_llm = should_not_call_llm
    summarized = asyncio.run(
        summarizer.summarize_metadata(sample_metadata(), data_source="demo", use_llm=True)
    )

    assert summarized["tables"][0]["summary"] == "第一次 LLM 摘要。"


def test_summarizer_refresh_forces_new_llm_summary(tmp_path):
    """refresh=True intentionally bypasses even a valid LLM cache entry."""
    summarizer = MetadataSummarizer(cache_path=str(tmp_path / "metadata_summaries.json"))
    summarizer._call_llm = lambda prompt: "旧 LLM 摘要。"
    asyncio.run(summarizer.summarize_metadata(sample_metadata(), data_source="demo", use_llm=True))

    summarizer._call_llm = lambda prompt: "刷新后的 LLM 摘要。"
    summarized = asyncio.run(
        summarizer.summarize_metadata(
            sample_metadata(),
            data_source="demo",
            use_llm=True,
            refresh=True,
        )
    )

    assert summarized["tables"][0]["summary"] == "刷新后的 LLM 摘要。"
