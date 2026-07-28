"""
SQL generator for OpenAI-compatible LLM endpoints.
Supports company LiteLLM, DashScope, XiYanSQL through Ollama, and XiYanSQL finetune service.
"""
import httpx
import json
import re
import time
from typing import Dict, Any, Optional, Tuple
from loguru import logger
from backend.config.config import settings
from backend.nl2sql.prompt_builder import PromptBuilder
from backend.nl2sql.table_selector import select_relevant_tables


class SQLGenerator:
    DEFAULT_MODEL_ID = "qwen3-coder-next-fp8"
    DASHSCOPE_MODEL_ID = "qwen3.7-max"
    XIYAN_OLLAMA_MODEL_ID = "xiyan-sql-3b-ollama"
    XIYAN_FINETUNE_MODEL_ID = "xiyan-sql-3b-finetune"
    # Backward compatible alias used by the first 3B integration.
    XIYAN_MODEL_ID = XIYAN_FINETUNE_MODEL_ID

    def __init__(self):
        self.prompt_builder = PromptBuilder()
        self.client = httpx.Client(timeout=getattr(settings, "llm_timeout", 15))
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self._sql_cache: dict[str, tuple[float, str, str, Dict[str, Any]]] = {}

    def _is_xiyan_model(self, model_id: Optional[str]) -> bool:
        return model_id in {
            self.XIYAN_OLLAMA_MODEL_ID,
            self.XIYAN_FINETUNE_MODEL_ID,
            "xiyan-sql-qwencoder-3b",
        }

    def _resolve_model_config(self, model_id: Optional[str] = None) -> tuple[str, str, str]:
        """Resolve front-end model_id to actual OpenAI-compatible endpoint and model name."""
        if model_id == self.XIYAN_OLLAMA_MODEL_ID:
            return (
                settings.xiyan_ollama_base_url.rstrip("/"),
                settings.xiyan_ollama_model,
                settings.xiyan_ollama_api_key,
            )
        if model_id in {self.XIYAN_FINETUNE_MODEL_ID, "xiyan-sql-qwencoder-3b"}:
            return (
                settings.xiyan_finetune_base_url.rstrip("/"),
                settings.xiyan_finetune_model,
                settings.xiyan_finetune_api_key,
            )
        if model_id == self.DASHSCOPE_MODEL_ID:
            return (
                settings.dashscope_base_url.rstrip("/"),
                settings.dashscope_model,
                settings.dashscope_api_key,
            )
        return (
            settings.litellm_base_url.rstrip("/"),
            settings.litellm_model,
            settings.litellm_api_key,
        )

    def _dialect_display_name(self, dialect: str) -> str:
        names = {
            "sqlite": "SQLite",
            "postgres": "PostgreSQL",
            "postgresql": "PostgreSQL",
            "mysql": "MySQL",
            "gauss": "PostgreSQL",
            "dameng": "MySQL",
            "hive": "Hive SQL",
        }
        return names.get((dialect or "").lower(), dialect or "SQL")

    async def generate_sql(self, question: str, metadata: Dict[str, Any],
                           dialect: str = "sqlite",
                           model_id: Optional[str] = None,
                           model_config: Optional[Dict[str, Any]] = None,
                           data_source: str = "") -> Tuple[str, str, Optional[str], Dict[str, Any]]:
        """
        Generate SQL for a given question and metadata.
        :param question:
        :param metadata:
        :param dialect:
        :param model_id:
        :param model_config:
        :return:
        """
        # 四张表足以覆盖当前主要多表链路，避免无关表扩大 Prompt。
        relevant_tables = select_relevant_tables(question, metadata, max_tables=4)
        trace = {
            "rag_enabled": False,
            "rag_hits": [],
            "selected_tables": relevant_tables,
            "prompt_token_estimate": max(1, len(prompt) // 4) if "prompt" in locals() else 0,
            "generation_cache_hit": False,
            "raw_model_output": "",
            "llm_thought": "",
            "schema_signature": metadata.get("schema_signature", ""),
        }
        cache_key = json.dumps(
            {
                "data_source": data_source,
                "question": question,
                "model_id": model_id,
                "config": model_config or {},
                "schema_signature": metadata.get("schema_signature", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        cached = self._sql_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            _, cached_sql, cached_thought, cached_trace = cached
            cached_trace = {
                **cached_trace,
                "selected_tables": relevant_tables,
                "generation_cache_hit": True,
                "raw_model_output": cached_trace.get("raw_model_output") or "[SQL_CACHE_HIT]",
                "llm_thought": cached_thought,
            }
            return cached_sql, cached_thought, None, cached_trace
        # XiYanSQL 是专用 Text-to-SQL 模型，prompt 模板必须严格匹配它的训练分布，跟通用大模型不能共用。
        # todo
        if self._is_xiyan_model(model_id):
            prompt = self.prompt_builder.build_xiyan_prompt(
                question,
                metadata,
                dialect=self._dialect_display_name(dialect),
                relevant_tables=relevant_tables,
            )

        else:
            prompt = self.prompt_builder.build_prompt(
                question,
                metadata,
                relevant_tables=relevant_tables,
                data_source=data_source,
            )
        trace["prompt_token_estimate"] = max(1, len(prompt) // 4)

        try:
            base_url, model, api_key = self._resolve_model_config(model_id)
            model_config = model_config or {}
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            response = self.client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是一个专业的SQL生成助手，严格遵守只读规则。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": float(model_config.get("temperature", 0.0 if self._is_xiyan_model(model_id) else 0.1)),
                    "top_p": float(model_config.get("top_p", 0.8)),
                    "max_tokens": min(
                        int(model_config.get("max_tokens", 512 if self._is_xiyan_model(model_id) else 1024)),
                        512 if self._is_xiyan_model(model_id) else 1024,
                    ),
                    "stream": False,
                },
                headers=headers,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            thought, sql = self._extract_thought_and_sql(content)
            trace["raw_model_output"] = content
            trace["llm_thought"] = thought
            if not sql:
                return "", thought or content, "未能从LLM响应中提取到有效SQL", trace
            clean_sql = self._clean_sql(sql)
            self._sql_cache[cache_key] = (time.monotonic() + settings.sql_generation_cache_seconds, clean_sql, thought, trace.copy())
            if len(self._sql_cache) > 256:
                self._sql_cache.pop(next(iter(self._sql_cache)))
            return clean_sql, thought, None, trace

        except httpx.HTTPStatusError as e:
            logger.error(f"LLM HTTP error: {e}")
            error_detail = e.response.text if e.response else str(e)
            return "", "", f"LLM服务调用失败: {error_detail}", trace
        except Exception as e:
            logger.error(f"LLM generation error: {e}")
            return "", "", f"SQL生成异常: {str(e)}", trace

    def _extract_thought_and_sql(self, content: str) -> Tuple[str, str]:
        """Extract LLM rationale and SQL from a completion."""
        thought = ""
        sql = ""
        sql_match = re.search(r"```sql\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
        if sql_match:
            sql = sql_match.group(1).strip()
            thought = content[:sql_match.start()].strip()
        else:
            select_match = re.search(r"((?:SELECT|WITH)\s+.*)", content, re.DOTALL | re.IGNORECASE)
            if select_match:
                sql = select_match.group(1).strip()
                thought = content[:select_match.start()].strip()
            else:
                thought = content
        return thought, sql

    async def summarize_result(
        self,
        question: str,
        columns: list[str],
        results: list[dict[str, Any]],
        answer_template: str = "brief",
        custom_instruction: str = "",
        model_id: Optional[str] = None,
        model_config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate the user-facing answer from verified result rows only."""
        template_answer = self._try_template_summary(question, columns, results)
        if template_answer:
            return template_answer

        templates = {
            "brief": "用 2 到 4 句话给出直接结论。",
            "analysis": "说明关键发现和可能的异常值。",
            "report": "以简短汇报摘要的方式组织。",
        }
        instruction = custom_instruction or "无"
        prompt = (
            "你是数据查询结果助手。只能依据下面已验证的查询结果回答，不要编造信息，不要输出思考过程、SQL、耗时或执行状态。\n"
            f"用户问题：{question}\n"
            f"字段：{columns}\n"
            f"查询结果（最多 20 行）：{results[:20]}\n"
            f"回答风格：{templates.get(answer_template, templates['brief'])}\n"
            f"用户自定义指令：{instruction}"
        )
        try:
            base_url, model, api_key = self._resolve_model_config(model_id)
            model_config = model_config or {}
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = self.client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": float(model_config.get("temperature", 0.2)),
                    "top_p": float(model_config.get("top_p", 0.8)),
                    "max_tokens": min(int(model_config.get("max_tokens", 512)), 1024),
                    "stream": False,
                },
                headers=headers,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            logger.warning(f"Result summary generation failed: {exc}")
            return f"查询完成，共返回 {len(results)} 条记录。"

    def _try_template_summary(
        self,
        question: str,
        columns: list[str],
        results: list[dict[str, Any]],
    ) -> str:
        """Return a deterministic summary for simple result shapes."""
        if not results:
            return "没有查询到符合条件的数据。"

        normalized_columns = [str(column) for column in columns]
        lower_columns = [column.lower() for column in normalized_columns]
        aggregate_keywords = (
            "count",
            "sum",
            "avg",
            "average",
            "max",
            "min",
            "数量",
            "总数",
            "总额",
            "平均",
            "最大",
            "最小",
            "合计",
        )

        if len(results) == 1:
            row = results[0]
            parts = []
            for key, value in row.items():
                if value is None:
                    continue
                if any(keyword in str(key).lower() for keyword in aggregate_keywords):
                    parts.append(f"{key}为 {value}")
            if parts:
                return "，".join(parts) + "。"

        if len(results) <= 10 and (
            any(keyword in question for keyword in ("前", "最高", "最低", "Top", "top", "排名"))
            or any("rank" in column or "排名" in column for column in lower_columns)
        ):
            preview = []
            for index, row in enumerate(results[:5], start=1):
                values = [f"{key}={value}" for key, value in list(row.items())[:3]]
                preview.append(f"{index}. " + "，".join(values))
            return f"查询到 {len(results)} 条记录，前几项为：" + "；".join(preview) + "。"

        if len(results) <= 5 and len(normalized_columns) <= 3:
            return f"查询到 {len(results)} 条记录，结果已在表格中展示。"

        return ""

    def _clean_sql(self, sql: str) -> str:
        """Clean markdown fences and keep the first statement only."""
        sql = re.sub(r"```.*?```", "", sql, flags=re.DOTALL).strip()
        if ";" in sql:
            sql = sql.split(";")[0] + ";"
        return sql.strip()

    async def self_correct_sql(self, original_sql: str, error_msg: str,
                               question: str, metadata: Dict[str, Any],
                               dialect: str = "sqlite",
                               model_id: Optional[str] = None,
                               model_config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """Ask the selected LLM to repair a failed read-only SQL statement."""
        correction_prompt = f"""之前的SQL执行失败了，请根据错误信息修复。

用户原始问题：{question}

之前生成的SQL：
```sql
{original_sql}
```

执行错误信息：
{error_msg}

请分析错误原因，然后生成修正后的正确SQL。保持只读规则（只允许 SELECT / WITH）。
只输出修正后的SQL（用```sql包裹）和简短修复说明。"""

        try:
            base_url, model, api_key = self._resolve_model_config(model_id)
            model_config = model_config or {}
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = self.client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": correction_prompt}],
                    "temperature": 0.0,
                    "top_p": float(model_config.get("top_p", 0.8)),
                    "max_tokens": min(int(model_config.get("max_tokens", 1024)), 2048),
                },
                headers=headers,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            _, corrected_sql = self._extract_thought_and_sql(content)
            return self._clean_sql(corrected_sql), content[:300]
        except Exception as e:
            logger.error(f"Self-correction failed: {e}")
            return "", str(e)
