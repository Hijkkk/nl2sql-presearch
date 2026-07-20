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
