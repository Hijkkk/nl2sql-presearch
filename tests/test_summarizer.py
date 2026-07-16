import asyncio
from backend.adapters.sqlite_adapter import SQLiteAdapter
from backend.nl2sql.metadata_summarizer import MetadataSummarizer


async def main():
    adapter = SQLiteAdapter()
    meta = adapter.get_metadata()

    summarizer = MetadataSummarizer()

    print("=== 原始 metadata 示例 ===")
    print(meta["tables"][0])  # 打印第一张表的原始信息

    print("\n=== 生成摘要后 ===")
    summarized = await summarizer.summarize_metadata(meta)
    print(summarized["tables"][0])  # 现在多了 "summary" 字段

    print("\n=== 缓存测试 ===")
    print(summarizer.get_cached_summary("employees"))


if __name__ == "__main__":
    asyncio.run(main())