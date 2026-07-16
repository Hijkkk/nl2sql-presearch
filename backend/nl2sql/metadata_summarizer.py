"""
元数据摘要/压缩机制 - 使用 LLM 生成表和字段的简短业务描述
用于减少 Prompt 长度（特别是对于大 schema 或小模型），提升 NL2SQL 效果
支持预生成摘要缓存，后续可持久化到文件或DB
"""
import httpx
from typing import Dict, Any, List, Optional
from loguru import logger
from backend.config.config import settings


class MetadataSummarizer:
    def __init__(self):
        self.client = httpx.Client(timeout=getattr(settings, 'llm_timeout', 120))
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.cache: Dict[str, str] = {}  # 简单内存缓存，key: table_name, value: summary

    def _call_llm(self, prompt: str) -> str:
        """内部调用 LLM"""
        try:
            headers = {
                "Content-Type": "application/json",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = self.client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "你是一个数据库 schema 专家，擅长生成简洁的业务描述。"},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 200,
                    "stream": False
                },
                headers=headers
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"MetadataSummarizer LLM call failed: {e}")
            return ""

    async def summarize_table(self, table_name: str, columns: List[Dict], comment: str = "") -> str:
        """
        为单个表生成简短摘要（1-2句业务描述 + 关键字段含义）
        """
        if table_name in self.cache:
            return self.cache[table_name]

        col_names = [col['name'] for col in columns]
        col_types = [f"{col['name']}({col['type']})" for col in columns[:5]]  # 限制前5个避免太长

        prompt = f"""请为以下数据库表生成一个简洁的业务描述（1-2句话），重点说明表的作用和关键字段含义。用于 NL2SQL 的 Prompt 优化。
表名: {table_name}
表注释: {comment or '无'}
字段: {', '.join(col_names)}
字段类型示例: {', '.join(col_types)}

输出格式：仅返回描述文本，不要解释。"""

        summary = self._call_llm(prompt)
        if summary:
            self.cache[table_name] = summary
        else:
            # 回退到简单描述
            summary = f"表 {table_name} 包含 {len(columns)} 个字段，主要用于存储相关业务数据。"
            self.cache[table_name] = summary
        return summary

    async def summarize_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        为整个 metadata 生成摘要版本
        返回类似原 metadata 但每个表有 'summary' 字段，columns 精简
        """
        summarized = {"tables": [], "total_tables": metadata.get("total_tables", 0)}
        
        for table in metadata.get("tables", []):
            table_name = table["name"]
            summary = await self.summarize_table(
                table_name, 
                table.get("columns", []), 
                table.get("comment", "")
            )
            
            # 精简 columns，只保留 name, type, comment（如果有）
            slim_columns = []
            for col in table.get("columns", []):
                slim_col = {
                    "name": col["name"],
                    "type": col["type"],
                }
                if col.get("comment"):
                    slim_col["comment"] = col["comment"]
                slim_columns.append(slim_col)
            
            summarized_table = {
                "name": table_name,
                "comment": table.get("comment", ""),
                "summary": summary,
                "columns": slim_columns,
                "primary_key": table.get("primary_key", []),
                "foreign_keys": table.get("foreign_keys", [])
            }
            summarized["tables"].append(summarized_table)
        
        return summarized

    def get_cached_summary(self, table_name: str) -> Optional[str]:
        return self.cache.get(table_name)

    def clear_cache(self):
        self.cache.clear()
