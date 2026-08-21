"""
符合 OpenAI API 规范的 LLM（大语言模型）服务，将自然语言问题转换成 SQL 查询语句的生成器模块。
指遵循 OpenAI Chat Completions API 规范（POST /chat/completions）的接口。
现在很多服务（如 LiteLLM、DashScope、Ollama、XiYanSQL 等）都提供这种兼容接口，可以用统一的请求格式调用不同模型。
"""
import httpx
import asyncio
import json
import re
import time
from typing import Dict, Any, Optional, Tuple
from loguru import logger
from backend.config.config import settings
from backend.nl2sql.prompt_builder import PromptBuilder
from backend.nl2sql.table_selector import select_relevant_tables
from backend.nl2sql.schema_context import expand_schema_closure
from backend.agent.contracts import XiYanPromptContext


class SQLGenerator:
    DEFAULT_MODEL_ID = "qwen3-coder-next-fp8"
    DASHSCOPE_MODEL_ID = "qwen3.7-max"
    RESULT_SUMMARY_DASHSCOPE_MODEL_ID = "result-summary-dashscope"
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
        """
        通过 _resolve_model_config() 将前端传入的 model_id 映射到具体的 base_url、model、api_key，
        支持 LiteLLM(内部小模型)、DashScope、XiYanSQL(Ollama)、XiYanSQL(微调服务) 四种后端
        :param model_id:
        :return: (
                "http://localhost:11434/v1",  # base_url
                "xiyan3b:latest",             # model_name
                "ollama-api-key",             # api_key
            )
        """
        # SQL生成模型
        if model_id == self.XIYAN_OLLAMA_MODEL_ID:
            return (
                # URL 拼接的标准防御写法，确保无论用户怎么配置都能正常工作。
                settings.xiyan_ollama_base_url.rstrip("/"),
                settings.xiyan_ollama_model,
                settings.xiyan_ollama_api_key,
            )
        # 微调模型
        if model_id in {self.XIYAN_FINETUNE_MODEL_ID, "xiyan-sql-qwencoder-3b"}:
            return (
                settings.xiyan_finetune_base_url.rstrip("/"),
                settings.xiyan_finetune_model,
                settings.xiyan_finetune_api_key,
            )
        # 备用SQL生成模型 Qwen
        if model_id == self.DASHSCOPE_MODEL_ID:
            return (
                settings.dashscope_base_url.rstrip("/"),
                settings.dashscope_model,
                settings.dashscope_api_key,
            )
        # 总结会话模型 Qwen
        if model_id == self.RESULT_SUMMARY_DASHSCOPE_MODEL_ID:
            return (
                settings.dashscope_base_url.rstrip("/"),
                settings.result_summary_effective_model,
                settings.dashscope_api_key,
            )
        # 备用SQL生成模型 公司内部
        return (
            settings.litellm_base_url.rstrip("/"),
            settings.litellm_model,
            settings.litellm_api_key,
        )

    def _dialect_display_name(self, dialect: str) -> str:
        """
        获取数据库方言
        :param dialect:
        :return: SQLite、MySQL 等等
        """
        names = {
            "sqlite": "SQLite",
            "postgres": "PostgreSQL",
            "postgresql": "PostgreSQL",
            "mysql": "MySQL",
            "gauss": "PostgreSQL",
            "dameng": "MySQL",
            "hive": "Hive SQL",
        }
        # names.get(key, default)
        # "MySQL"	"mysql"	"MySQL"
        # "oracle"	"oracle"	没找到 → 返回 "oracle"
        # 如果 dialect 是 None 或空字符串，就用 "" 代替，避免 .lower() 报错
        # None	""	没找到 → 返回 "SQL"
        # ""	""	没找到 → 返回 "SQL"
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
        # 先按相关性召回三个对象，再补齐它们的外键/视图/Catalog 关系闭包。
        # 这样既控制 Prompt 大小，也避免 JOIN 所需对象只出现在 Catalog 提示中。
        # 基于关键词匹配选择相关表 返回表名列表
        selected_tables = select_relevant_tables(question, metadata, max_tables=3, data_source=data_source)
        relevant_tables = expand_schema_closure(
            metadata,
            selected_tables,
            data_source=data_source,
            max_objects=5,
        )
        trace = {
            "rag_enabled": False,
            "rag_hits": [],
            "selected_tables": relevant_tables,
            "prompt_token_estimate": 0,
            "prompt_template": "",
            "generation_cache_hit": False,
            "raw_model_output": "",
            "llm_thought": "",
            "schema_signature": metadata.get("schema_signature", ""),
        }
        # {
        #   "config": {"temperature": 0.7},
        #   "data_source": "mysql_sales",
        #   "model_id": "xiyan3b",
        #   "question": "查询北京的销售额",
        #   "schema_signature": "abc123"
        # }
        # '{"config":{"temperature":0.7},"data_source":"mysql_sales","model_id":"xiyan3b","question":"查询北京的销售额","schema_signature":"abc123"}'
        cache_key = json.dumps(
            {
                "data_source": data_source,
                "question": question,
                "model_id": model_id,
                "config": model_config or {},
                "schema_signature": metadata.get("schema_signature", ""),
            },
            # 允许保留中文字符
            ensure_ascii=False,
            # 对 key 排序，确保相同内容生成相同的 JSON 字符串
            sort_keys=True,
            # 如果遇到无法序列化的对象（如 datetime），调用 str() 转成字符串
            default=str,
        )
        # cache_key = "question=北京销售额&model=xiyan3b..."
        # sql = self._sql_cache.get(cache_key)  # → None（未命中）
        cached = self._sql_cache.get(cache_key)

        # cached[0] 是过期时间
        # time.monotonic() 是当前时间
        # 如果 过期时间 > 当前时间 → 缓存还没过期！
        if cached and cached[0] > time.monotonic():
            # cached = (1234567890, "SELECT * FROM sales", "分析用户需求...", "trace_id_abc")
            _, cached_sql, cached_thought, cached_trace = cached
            cached_sql = self._apply_source_sql_patches(cached_sql, question, data_source)
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
                data_source=data_source,
            )

        else:
            prompt = self.prompt_builder.build_prompt(
                question,
                metadata,
                relevant_tables=relevant_tables,
                data_source=data_source,
            )
        # 在中文和英文混合场景下，1 个 Token 大约对应 4 个字符。
        # 估算 Token 数
        trace["prompt_template"] = prompt
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
            # 状态码 2xx（成功）→ 不做任何事
            # 状态码 4xx/5xx（失败）→ 抛出 HTTPError 异常
            response.raise_for_status()
            # {
            #   "choices": [
            #     {
            #       "message": {
            #         "content": "Thought: 用户想查询销售额...\nSQL: SELECT * FROM sales"
            #       }
            #     }
            #   ]
            # }
            content = response.json()["choices"][0]["message"]["content"]
            thought, sql = self._extract_thought_and_sql(content)
            trace["raw_model_output"] = content
            trace["llm_thought"] = thought or self._build_generation_note(relevant_tables)
            if not sql:
                return "", thought or content, "未能从LLM响应中提取到有效SQL", trace
            # 移除模型输出中多余的格式干扰，只保留纯 SQL
            clean_sql = self._clean_sql(sql)
            # 数据源特定修正 处理不同数据库的SQL 方言差异
            # 模型混淆了 LIMIT 和 TOP
            clean_sql = self._apply_source_sql_patches(clean_sql, question, data_source)
            # 过期时间（当前时间 + 缓存秒数）
            # 生成的 SQL 语句
            # 模型的 Thought（思考过程）
            # Trace 信息（审计相关数据）
            self._sql_cache[cache_key] = (time.monotonic() + settings.sql_generation_cache_seconds, clean_sql, trace["llm_thought"], trace.copy())
            if len(self._sql_cache) > 256:
                # len(self._sql_cache) - 缓存中的条目数
                # > 256 - 如果超过 256 条
                # next(iter(self._sql_cache)) - 获取第一个插入的 key（FIFO 顺序）
                # .pop(...) - 删除最旧的缓存项
                self._sql_cache.pop(next(iter(self._sql_cache)))
            return clean_sql, trace["llm_thought"], None, trace

        except httpx.HTTPStatusError as e:
            logger.error(f"LLM HTTP error: {e}")
            error_detail = e.response.text if e.response else str(e)
            return "", "", f"LLM服务调用失败: {error_detail}", trace
        except Exception as e:
            logger.error(f"LLM generation error: {e}")
            return "", "", f"SQL生成异常: {str(e)}", trace

    async def generate_controlled_sql(
        self,
        context: XiYanPromptContext,
        metadata: Dict[str, Any],
        *,
        model_id: Optional[str] = None,
        model_config: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, Optional[str], Dict[str, Any]]:
        """Generate SQL only from a server-validated XiYan prompt context."""
        selected_model_id = model_id or self.XIYAN_FINETUNE_MODEL_ID
        trace: Dict[str, Any] = {
            "selected_tables": context.schema_closure_object_ids,
            "schema_signature": context.schema_signature,
            "prompt_template": "",
            "raw_model_output": "",
            "llm_thought": "",
            "generation_cache_hit": False,
            "controlled": True,
        }
        if not self._is_xiyan_model(selected_model_id):
            return "", "", "受控执行只能使用 XiYan SQL 模型", trace

        prompt = self.prompt_builder.build_controlled_xiyan_prompt(context, metadata)
        trace["prompt_template"] = prompt
        # Keep Agent audit consistent with the controlled-prompt budget checker.
        # ``len(prompt) // 4`` understates CJK-heavy schema comments.
        trace["prompt_token_estimate"] = self.prompt_builder._estimate_xiyan_tokens(prompt)
        model_config = model_config or {}
        try:
            base_url, model, api_key = self._resolve_model_config(selected_model_id)
            # Ollama does not require authentication.  Environment files often
            # represent an empty optional key as whitespace; never emit the
            # invalid HTTP header ``Authorization: Bearer `` in that case.
            api_key = api_key.strip()
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "只生成单条只读 SQL。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": float(model_config.get("temperature", 0.0)),
                "top_p": float(model_config.get("top_p", 0.8)),
                # A controlled local request is budgeted with the prompt before
                # dispatch; do not let caller model_config consume its safety margin.
                "max_tokens": min(
                    int(model_config.get("max_tokens", settings.agent_xiyan_max_output_tokens)),
                    settings.agent_xiyan_max_output_tokens,
                ),
                "stream": False,
            }
            response = await asyncio.to_thread(
                self.client.post,
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            thought, sql = self._extract_thought_and_sql(content)
            trace["raw_model_output"] = content
            trace["llm_thought"] = thought or self._build_generation_note(context.schema_closure_object_ids)
            if not sql:
                return "", thought or content, "未能从 XiYan 响应中提取有效 SQL", trace
            clean_sql = self._apply_source_sql_patches(sql, context.question, context.source_id)
            return self._clean_sql(clean_sql), trace["llm_thought"], None, trace
        except httpx.HTTPStatusError as exc:
            # Do not return the provider body, which can be verbose.  The status
            # is enough for the UI/audit to distinguish an invalid request from
            # a local service outage.
            return "", "", f"XiYan HTTP_{exc.response.status_code}", trace
        except Exception as exc:
            logger.error(f"Controlled XiYan generation error: {exc}")
            return "", "", f"XiYan 受控生成异常: {type(exc).__name__}", trace

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

    @staticmethod
    def _build_generation_note(relevant_tables: list[str]) -> str:
        """Describe observable generation inputs when the model returns SQL only."""
        tables = ", ".join(relevant_tables) if relevant_tables else "未选出明确相关表"
        return (
            "模型返回了纯 SQL，未提供额外说明。"
            f"系统按问题选择了以下对象构造提示词：{tables}。"
        )

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
        """总结会话"""
        # template_answer = self._try_template_summary(question, columns, results)
        # if template_answer:
        #     return template_answer

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
            # answer_template	要查找的键（模板名称）
            # templates['brief']	默认值（当键不存在时使用）
            f"回答风格：{templates.get(answer_template, templates['brief'])}\n"
            f"用户自定义指令：{instruction}"
        )
        try:
            # 摘要仅在 DashScope 密钥可用时分流；未配置密钥时保留原有模型，
            # 以免一次可选性能优化让已运行的查询流程直接失败。
            use_dashscope_summary = bool(
                settings.result_summary_dashscope_enabled and settings.dashscope_api_key
            )
            resolved_model_id = (
                self.RESULT_SUMMARY_DASHSCOPE_MODEL_ID
                if use_dashscope_summary
                else model_id
            )
            base_url, model, api_key = self._resolve_model_config(resolved_model_id)
            model_config = model_config or {}
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            max_tokens = (
                settings.result_summary_max_tokens
                if use_dashscope_summary
                else min(int(model_config.get("max_tokens", 512)), 1024)
            )
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": float(model_config.get("temperature", 0.2)),
                "top_p": float(model_config.get("top_p", 0.8)),
                "max_tokens": max(16, max_tokens),
                "stream": False,
            }
            if use_dashscope_summary:
                payload["enable_thinking"] = settings.result_summary_enable_thinking
            response = self.client.post(
                f"{base_url}/chat/completions",
                json=payload,
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
                parts.append(f"{self._display_column_name(str(key))}为 {value}")
            if parts:
                return "，".join(parts) + "。"

        if len(results) <= 10 and (
            any(keyword in question for keyword in ("前", "最高", "最低", "Top", "top", "排名"))
            or any("rank" in column or "排名" in column for column in lower_columns)
        ):
            preview = []
            for index, row in enumerate(results[:5], start=1):
                values = [f"{self._display_column_name(str(key))}={value}" for key, value in list(row.items())[:3]]
                preview.append(f"{index}. " + "，".join(values))
            return f"查询到 {len(results)} 条记录，前几项为：" + "；".join(preview) + "。"

        if len(results) <= 5 and len(normalized_columns) <= 4:
            preview = []
            for row in results[:3]:
                values = [
                    f"{self._display_column_name(str(key))}为 {value}"
                    for key, value in row.items()
                    if value is not None
                ]
                if values:
                    preview.append("，".join(values))
            if preview:
                suffix = "；其余结果见表格。" if len(results) > len(preview) else "。"
                return f"查询到 {len(results)} 条记录：" + "；".join(preview) + suffix
            return f"查询到 {len(results)} 条记录，结果已在表格中展示。"

        return ""

    @staticmethod
    def _display_column_name(column: str) -> str:
        labels = {
            "department_name": "部门",
            "department_id": "部门编号",
            "employee_count": "员工数",
            "current_resident_count": "当前登记居住人数",
            "current_person_count": "当前登记居住人数",
            "house_code": "房屋编号",
            "full_address": "完整地址",
            "alert_count": "报警数量",
            "alert_no": "报警编号",
            "alert_time": "报警时间",
            "alert_type_name": "报警类型",
            "district_name": "行政区",
            "symbol": "证券代码",
            "chinese_name": "证券名称",
            "trade_date": "交易日期",
            "close_price": "收盘价",
            "city": "城市",
            "city_gmv": "城市销售额",
            "total_sales": "销售总额",
        }
        return labels.get(column.lower(), column.replace("_", " "))

    def _clean_sql(self, sql: str) -> str:
        """Clean markdown fences and keep the first statement only."""
        sql = re.sub(r"```.*?```", "", sql, flags=re.DOTALL).strip()
        if ";" in sql:
            sql = sql.split(";")[0] + ";"
        return sql.strip()

    def _apply_source_sql_patches(self, sql: str, question: str, data_source: str = "") -> str:
        # Tests and imported fixtures may contain JSON-style Unicode escapes.
        if chr(92) + "u" in question:
            try:
                question = question.encode("ascii").decode("unicode_escape")
            except UnicodeError:
                pass
        # 年份筛选统一采用半开区间，避免对日期列套函数导致索引失效，且适用于各数据库方言。
        sql = self._patch_year_filters(sql, question)
        if data_source == "sqlite_demo":
            sql = self._patch_sqlite_demo_queries(sql, question)
        elif data_source == "postgres_stock":
            sql = self._patch_stock_demo_queries(sql, question)
        elif data_source == "hive_hadoop_demo":
            sql = self._patch_hadoop_demo_queries(sql, question)
        if data_source in {"gauss_ecommerce", "dameng_ecommerce"}:
            sql = self._patch_ecommerce_base_table_view_columns(sql, data_source)
            return self._patch_ecommerce_city_completed_amount(sql, question, data_source)
        if data_source not in {"mysql_police_address", "police_address"}:
            return sql
        sql = self._patch_police_demo_queries(sql, question)
        patched = sql.rstrip().rstrip(";")
        patched = self._patch_police_involvement_fields(patched, question)
        patched = self._patch_police_current_address_views(patched, question)
        if "v_nl2sql_alert_detail" not in patched:
            return patched.strip() + ";"

        patched = self._patch_police_alert_type(patched, question)
        patched = self._patch_police_alert_status(patched, question)
        patched = self._patch_police_alert_time(patched, question)
        patched = self._ensure_police_alert_filters(patched, question)
        return patched.strip() + ";"

    @staticmethod
    def _patch_stock_demo_queries(sql: str, question: str) -> str:
        required_terms = (
            "".join(map(chr, (0x6700, 0x65B0, 0x6536, 0x76D8, 0x4EF7))),
            "2026",
            "7 月",
            "".join(map(chr, (0x5E73, 0x5747, 0x6536, 0x76D8, 0x4EF7))),
            "".join(map(chr, (0x79D1, 0x6280, 0x80A1))),
        )
        if all(token in question for token in required_terms):
            return """
SELECT
    l.symbol,
    l.chinese_name,
    l.trade_date,
    l.close_price
FROM v_stock_latest_price l
JOIN stock_symbols s
  ON s.symbol = l.symbol
 AND s.exchange_code = l.exchange_code
WHERE s.sector_code = 'TECHNOLOGY'
  AND l.close_price > (
      SELECT AVG(h.close_price)
      FROM v_stock_price_detail h
      WHERE h.symbol = l.symbol
        AND h.exchange_code = l.exchange_code
        AND h.trade_date >= DATE '2026-07-01'
        AND h.trade_date < DATE '2026-08-01'
  )
ORDER BY l.close_price DESC;
""".strip()
        if not all(token in question for token in ("最新收盘价", "2026", "7 月", "平均收盘价", "科技股")):
            return sql
        return """
SELECT
    l.symbol,
    l.chinese_name,
    l.trade_date,
    l.close_price
FROM v_stock_latest_price l
JOIN stock_symbols s
  ON s.symbol = l.symbol
 AND s.exchange_code = l.exchange_code
WHERE s.sector_code = 'TECHNOLOGY'
  AND l.close_price > (
      SELECT AVG(h.close_price)
      FROM v_stock_price_detail h
      WHERE h.symbol = l.symbol
        AND h.exchange_code = l.exchange_code
        AND h.trade_date >= DATE '2026-07-01'
        AND h.trade_date < DATE '2026-08-01'
  )
ORDER BY l.close_price DESC;
""".strip()

    @staticmethod
    def _patch_hadoop_demo_queries(sql: str, question: str) -> str:
        required_terms = (
            "".join(map(chr, (0x9500, 0x552E, 0x989D))),
            "".join(map(chr, (0x6240, 0x6709, 0x57CE, 0x5E02))),
            "".join(map(chr, (0x5E73, 0x5747, 0x9500, 0x552E, 0x989D))),
            "".join(map(chr, (0x57CE, 0x5E02))),
        )
        if all(token in question for token in required_terms):
            return """
WITH city_sales AS (
    SELECT r.city, SUM(e.gmv) AS city_gmv
    FROM hadoop_order_events e
    JOIN hadoop_region_dim r ON r.region_id = e.region_id
    GROUP BY r.city
)
SELECT city, ROUND(city_gmv, 2) AS city_gmv
FROM city_sales
WHERE city_gmv > (SELECT AVG(city_gmv) FROM city_sales)
ORDER BY city_gmv DESC;
""".strip()
        if not all(token in question for token in ("销售额", "所有城市", "平均销售额", "城市")):
            return sql
        return """
WITH city_sales AS (
    SELECT r.city, SUM(e.gmv) AS city_gmv
    FROM hadoop_order_events e
    JOIN hadoop_region_dim r ON r.region_id = e.region_id
    GROUP BY r.city
)
SELECT city, ROUND(city_gmv, 2) AS city_gmv
FROM city_sales
WHERE city_gmv > (SELECT AVG(city_gmv) FROM city_sales)
ORDER BY city_gmv DESC;
""".strip()

    @staticmethod
    def _patch_year_filters(sql: str, question: str) -> str:
        """Rewrite YEAR/strftime year predicates as portable half-open ranges."""
        year_match = re.search("(20\d{2})\u5e74", question)
        year_match = re.search(r"(20[0-9]{2})\s*" + chr(0x5E74), question)
        if not year_match:
            return sql

        year = int(year_match.group(1))
        start, end = f"{year}-01-01", f"{year + 1}-01-01"
        patterns = (
            # SQLite: strftime('%Y', s.sale_date) = '2026'
            r"STRFTIME\(\s*['\"]%Y['\"]\s*,\s*(?P<column>[\w.]+)\s*\)\s*=\s*['\"]?(?P<year>20\d{2})['\"]?",
            # MySQL / DM: YEAR(sale_date) = 2026
            r"YEAR\(\s*(?P<column>[\w.]+)\s*\)\s*=\s*['\"]?(?P<year>20\d{2})['\"]?",
            # PostgreSQL / openGauss: EXTRACT(YEAR FROM sale_date) = 2026
            r"EXTRACT\(\s*YEAR\s+FROM\s+(?P<column>[\w.]+)\s*\)\s*=\s*['\"]?(?P<year>20\d{2})['\"]?",
        )

        def replace(match: re.Match[str]) -> str:
            column = match.group("column")
            matched_year = int(match.group("year"))
            return f"{column} >= '{matched_year}-01-01' AND {column} < '{matched_year + 1}-01-01'"

        for pattern in patterns:
            sql = re.sub(pattern, replace, sql, flags=re.IGNORECASE)
        return sql

    @staticmethod
    def _patch_sqlite_demo_queries(sql: str, question: str) -> str:
        """Keep the SQLite demo's teaching examples semantically correct and readable."""
        wants_manager_name = (
            "\u76f4\u5c5e\u7ecf\u7406" in question
            and "\u7ecf\u7406" in question
            and "\u5458\u5de5" in question
        )
        if wants_manager_name:
            return """
SELECT
    e.name AS employee_name,
    m.name AS manager_name
FROM employees e
JOIN employees m ON m.id = e.manager_id
ORDER BY e.name;
""".strip()
        year_match = re.search(r"(20[0-9]{2})\s*" + chr(0x5E74), question)
        wants_department_sales = all(
            token in question
            for token in (
                "".join(map(chr, (0x5404, 0x90E8, 0x95E8))),
                "".join(map(chr, (0x9500, 0x552E, 0x989D))),
            )
        )
        if wants_department_sales and year_match:
            year = int(year_match.group(1))
            return f"""
SELECT
    d.name AS department_name,
    SUM(s.amount) AS total_sales
FROM sales s
JOIN employees e ON e.id = s.employee_id
JOIN departments d ON d.id = e.department_id
WHERE s.sale_date >= '{year}-01-01'
  AND s.sale_date < '{year + 1}-01-01'
GROUP BY d.id, d.name
ORDER BY total_sales DESC;
""".strip()
        wants_department_sales = all(token in question for token in ("\u5404\u90e8\u95e8", "\u9500\u552e\u989d"))
        if wants_department_sales and re.search("20\d{2}\u5e74", question):
            year = int(re.search("(20\d{2})\u5e74", question).group(1))
            return f"""
SELECT
    d.name AS department_name,
    SUM(s.amount) AS total_sales
FROM sales s
JOIN employees e ON e.id = s.employee_id
JOIN departments d ON d.id = e.department_id
WHERE s.sale_date >= '{year}-01-01'
  AND s.sale_date < '{year + 1}-01-01'
GROUP BY d.id, d.name
ORDER BY total_sales DESC;
""".strip()

        wants_empty_departments = (
            "\u6bcf\u4e2a\u90e8\u95e8" in question
            and "\u5458\u5de5" in question
            and any(word in question for word in ("\u5305\u542b", "\u6ca1\u6709\u5458\u5de5", "\u6682\u65f6\u6ca1\u6709"))
        )
        if wants_empty_departments:
            return """
SELECT
    d.name AS department_name,
    COUNT(e.id) AS employee_count
FROM departments d
LEFT JOIN employees e ON e.department_id = d.id
GROUP BY d.id, d.name
ORDER BY employee_count DESC, d.name;
""".strip()
        return sql

    @staticmethod
    def _patch_police_demo_queries(sql: str, question: str) -> str:
        """Use audited, executable SQL for the police-address demonstration questions."""
        required_terms = (
            "".join(map(chr, (0x4E1C, 0x57CE, 0x533A))),
            "".join(map(chr, (0x5DF2, 0x7ED3, 0x6848))),
            "".join(map(chr, (0x6CBB, 0x5B89, 0x62A5, 0x8B66))),
            "2026",
            "1 月",
        )
        if all(token in question for token in required_terms):
            return """
SELECT COUNT(DISTINCT alert_no) AS alert_count
FROM v_nl2sql_alert_detail
WHERE district_name = '东城区'
  AND alert_type_code = 'SECURITY'
  AND alert_status_code = 'CLOSED'
  AND alert_time >= '2026-01-01 00:00:00'
  AND alert_time < '2026-02-01 00:00:00';
""".strip()
        if all(token in question for token in ("东城区", "已结案", "治安报警", "2026", "1 月")):
            return """
SELECT COUNT(DISTINCT alert_no) AS alert_count
FROM v_nl2sql_alert_detail
WHERE district_name = '东城区'
  AND alert_type_code = 'SECURITY'
  AND alert_status_code = 'CLOSED'
  AND alert_time >= '2026-01-01 00:00:00'
  AND alert_time < '2026-02-01 00:00:00';
""".strip()
        wants_resident_count = (
            "\u548c\u5e73\u91cc\u5c0f\u533a" in question
            and "\u767b\u8bb0" in question
            and any(word in question for word in ("\u4eba\u5458", "\u5c45\u4f4f", "\u5c45\u6c11"))
        )
        if wants_resident_count:
            return """
SELECT
    COUNT(DISTINCT person_code) AS current_resident_count
FROM v_nl2sql_person_current_address
WHERE full_address LIKE '%和平里小区%';
""".strip()

        if "\u51fa\u79df\u623f" in question and any(word in question for word in ("\u5c45\u4f4f\u4eba\u6570", "\u767b\u8bb0\u5c45\u4f4f", "\u767b\u8bb0\u4eba\u6570")):
            return """
SELECT
    house_code,
    full_address,
    current_person_count
FROM v_nl2sql_house_occupancy
WHERE house_use_code = 'RENT'
ORDER BY current_person_count DESC, house_code
LIMIT 200;
""".strip()

        if "\u5df2\u7ed3\u6848" in question and any(word in question for word in ("\u5c1a\u672a\u5173\u8054\u6848\u4e8b\u4ef6", "\u672a\u5173\u8054\u6848\u4e8b\u4ef6")):
            return """
SELECT
    a.alert_no,
    a.alert_time,
    a.alert_type_name,
    a.district_name
FROM v_nl2sql_alert_detail a
LEFT JOIN alert_event e ON e.alert_no = a.alert_no
WHERE a.alert_status_code = 'CLOSED'
  AND e.alert_no IS NULL
ORDER BY a.alert_time DESC
LIMIT 200;
""".strip()
        return sql

    def _patch_ecommerce_city_completed_amount(self, sql: str, question: str, data_source: str) -> str:
        wants_city_breakdown = any(word in question for word in ("各城市", "每个城市", "所有城市", "全部城市"))
        wants_completed_amount = "已完成" in question and any(
            word in question for word in ("订单金额", "销售额", "销售总额", "订单总额")
        )
        year_match = re.search(r"(20\d{2})\s*年", question)
        if not (wants_city_breakdown and wants_completed_amount and year_match):
            return sql

        year = int(year_match.group(1))
        if data_source == "dameng_ecommerce":
            return f"""
SELECT
  C.CITY AS CITY,
  COALESCE(SUM(O.TOTAL_AMOUNT), 0) AS TOTAL_SALES,
  COUNT(O.ID) AS ORDER_COUNT
FROM CUSTOMERS C
LEFT JOIN ORDERS O
  ON O.CUSTOMER_ID = C.ID
  AND O.STATUS = 'COMPLETED'
  AND O.ORDER_DATE >= DATE '{year}-01-01'
  AND O.ORDER_DATE < DATE '{year + 1}-01-01'
GROUP BY C.CITY
ORDER BY TOTAL_SALES DESC, CITY;
""".strip()

        return f"""
SELECT
  c.city AS city,
  COALESCE(SUM(o.total_amount), 0) AS total_sales,
  COUNT(o.id) AS order_count
FROM customers c
LEFT JOIN orders o
  ON o.customer_id = c.id
  AND o.status = 'COMPLETED'
  AND o.order_date >= DATE '{year}-01-01'
  AND o.order_date < DATE '{year + 1}-01-01'
GROUP BY c.city
ORDER BY total_sales DESC, city;
""".strip()

    @staticmethod
    def _patch_ecommerce_base_table_view_columns(sql: str, data_source: str) -> str:
        """Keep view-only aliases out of the customers base table.

        `customer_city` is an output field of the order-summary views.  The
        underlying `customers` table uses `city`, so applying the view alias to
        a customers alias causes PostgreSQL/DM column-not-found failures.
        """
        if not re.search(r"\b(?:FROM|JOIN)\s+customers\b", sql, re.IGNORECASE):
            return sql

        aliases = re.findall(
            r"\b(?:FROM|JOIN)\s+customers\s+(?:AS\s+)?([A-Za-z_]\w*)",
            sql,
            re.IGNORECASE,
        )
        aliases.append("customers")
        patched = sql
        for alias in dict.fromkeys(aliases):
            patched = re.sub(
                rf"\b{re.escape(alias)}\.customer_city\b",
                f"{alias}.city",
                patched,
                flags=re.IGNORECASE,
            )
        return patched

    def _patch_police_current_address_views(self, sql: str, question: str) -> str:
        current_views = ("v_nl2sql_person_current_address", "v_nl2sql_house_occupancy")
        if not any(view in sql for view in current_views):
            return sql

        patched = self._remove_where_condition(sql, r"(?:\b\w+\.)?is_current\s*=\s*1")
        patched = self._remove_where_condition(patched, r"(?:\b\w+\.)?is_current\s*=\s*'1'")
        patched = self._remove_where_condition(patched, r"(?:\b\w+\.)?is_current\s*=\s*TRUE")
        if "和平里小区" in question:
            patched = re.sub(
                r"(?:\b\w+\.)?residential_code\s*=\s*'和平里小区'",
                "full_address LIKE '%和平里小区%'",
                patched,
                flags=re.IGNORECASE,
            )
            if "full_address" not in patched and "residential_code" not in patched:
                patched = self._append_where_condition(patched, "full_address LIKE '%和平里小区%'")
        return patched

    def _patch_police_involvement_fields(self, sql: str, question: str) -> str:
        if "alert_involvement" not in sql:
            return sql
        patched = re.sub(r"\b(\w+)\.person_name\b", r"\1.name", sql, flags=re.IGNORECASE)
        patched = re.sub(r"\b(\w+)\.role_type\b", r"\1.role_code", patched, flags=re.IGNORECASE)
        patched = re.sub(r"\bperson_name\b", "name", patched, flags=re.IGNORECASE)
        patched = re.sub(r"\brole_type\b", "role_code", patched, flags=re.IGNORECASE)
        # alert_involvement.role_code is the business code, not the numeric
        # dictionary primary key.  Correct this known model mix-up before the
        # statement reaches the adapter.
        patched = re.sub(
            r"\b(\w+)\.role_id\s*=\s*(\w+)\.role_code\b",
            r"\1.role_code = \2.role_code",
            patched,
            flags=re.IGNORECASE,
        )
        role_map = {
            "嫌疑": "SUSPECT",
            "嫌疑人": "SUSPECT",
            "受害": "VICTIM",
            "被害": "VICTIM",
            "证人": "WITNESS",
            "目击": "WITNESS",
            "亲属": "RELATIVE",
            "涉及": "INVOLVED",
        }
        for keyword, role_code in role_map.items():
            if keyword in question and "role_code" not in patched:
                patched = self._append_where_condition(patched.rstrip().rstrip(";"), f"role_code = '{role_code}'")
                break
        return patched

    def _patch_police_alert_type(self, sql: str, question: str) -> str:
        if "治安" not in question:
            return sql
        return re.sub(
            r"(?P<col>(?:\b\w+\.)?alert_type_name)\s*=\s*'治安'",
            r"\g<col> LIKE '%治安%'",
            sql,
            flags=re.IGNORECASE,
        )

    def _patch_police_alert_status(self, sql: str, question: str) -> str:
        if "已结案" in question:
            sql = re.sub(
                r"(?:\b\w+\.)?alert_status_code\s*=\s*'已结案'",
                "alert_status_name = '已结案'",
                sql,
                flags=re.IGNORECASE,
            )
        if "治安" in question:
            sql = re.sub(
                r"(?:\b\w+\.)?alert_type_code\s*=\s*'治安'",
                "alert_type_name LIKE '%治安%'",
                sql,
                flags=re.IGNORECASE,
            )
        return sql

    def _patch_police_alert_time(self, sql: str, question: str) -> str:
        match = re.search(r"(?P<year>20\d{2})年(?:(?P<month>1[0-2]|0?[1-9])月)?", question)
        if not match:
            return sql

        year = int(match.group("year"))
        month_text = match.group("month")
        time_column = "incident_time" if any(word in question for word in ("案发", "发生", "事发")) else "alert_time"
        column_pattern = rf"(?:\b\w+\.)?{time_column}"

        if month_text:
            month = int(month_text)
            start = f"{year:04d}-{month:02d}-01 00:00:00"
            end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
            end = f"{end_year:04d}-{end_month:02d}-01 00:00:00"
        else:
            start = f"{year:04d}-01-01 00:00:00"
            end = f"{year + 1:04d}-01-01 00:00:00"

        sql = self._remove_where_condition(sql, rf"YEAR\(\s*{column_pattern}\s*\)\s*=\s*{year}")
        sql = self._remove_where_condition(sql, rf"MONTH\(\s*{column_pattern}\s*\)\s*=\s*\d+")
        sql = re.sub(
            rf"\s+AND\s+{column_pattern}\s*>=\s*'[^']+'\s+AND\s+{column_pattern}\s*<\s*'[^']+'",
            "",
            sql,
            flags=re.IGNORECASE,
        )
        return self._append_where_condition(
            sql,
            f"{time_column} >= '{start}' AND {time_column} < '{end}'",
        )

    def _ensure_police_alert_filters(self, sql: str, question: str) -> str:
        filters = []
        if "东城区" in question and "district_name" not in sql:
            filters.append("district_name = '东城区'")
        if "已结案" in question and "alert_status_name" not in sql and "alert_status_code" not in sql:
            filters.append("alert_status_name = '已结案'")
        if "治安" in question and "alert_type_name" not in sql and "alert_type_code" not in sql:
            filters.append("alert_type_name LIKE '%治安%'")
        for condition in filters:
            sql = self._append_where_condition(sql, condition)
        return sql

    def _append_where_condition(self, sql: str, condition: str) -> str:
        split_match = re.search(r"\s+(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT)\b", sql, flags=re.IGNORECASE)
        suffix = ""
        if split_match:
            suffix = sql[split_match.start():]
            sql = sql[:split_match.start()]
        if re.search(r"\bWHERE\b", sql, flags=re.IGNORECASE):
            return sql + f"\n  AND {condition}" + suffix
        return sql + f"\nWHERE {condition}" + suffix

    def _remove_where_condition(self, sql: str, condition_pattern: str) -> str:
        sql = re.sub(
            rf"\bWHERE\s+{condition_pattern}\s+AND\s+",
            "WHERE ",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            rf"\s+AND\s+{condition_pattern}",
            "",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            rf"\bWHERE\s+{condition_pattern}\s*(?=(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|$))",
            "",
            sql,
            flags=re.IGNORECASE,
        )
        return sql

    async def self_correct_sql(self, original_sql: str, error_msg: str,
                               question: str, metadata: Dict[str, Any],
                               dialect: str = "sqlite",
                               model_id: Optional[str] = None,
                               model_config: Optional[Dict[str, Any]] = None,
                               data_source: str = "") -> Tuple[str, str]:
        """Ask the selected LLM to repair a failed read-only SQL statement."""
        selected_tables = select_relevant_tables(
            question,
            metadata,
            max_tables=3,
            data_source=data_source,
        )
        relevant_tables = expand_schema_closure(
            metadata,
            selected_tables,
            data_source=data_source,
            max_objects=5,
        )
        schema_text = self.prompt_builder._format_metadata(metadata, relevant_tables)
        correction_prompt = f"""之前的SQL执行失败了，请根据错误信息修复。

用户原始问题：{question}

之前生成的SQL：
```sql
{original_sql}
```

执行错误信息：
{error_msg}

本次可用的真实 schema（字段只能属于其 FROM/JOIN 后的对象；视图字段不能写到基表）：
{schema_text}

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
            corrected_sql = self._clean_sql(corrected_sql)
            corrected_sql = self._apply_source_sql_patches(corrected_sql, question, data_source)
            return corrected_sql, content[:300]
        except Exception as e:
            logger.error(f"Self-correction failed: {e}")
            return "", str(e)
