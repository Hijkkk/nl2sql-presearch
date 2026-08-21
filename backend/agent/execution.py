"""Guarded single-source execution for plans approved by the controlled graph."""
from __future__ import annotations

import asyncio
import re
from typing import Protocol

import sqlglot
from sqlglot import exp

from backend.adapters.registry import get_adapter
from backend.agent.contracts import AgentExecutionResult, AgentPlan, MetadataContext, SqlRepairProposal, XiYanPromptContext
from backend.agent.llm import AgentLLMError, QwenSqlDiagnostician, QwenSqlReviewer
from backend.config.config import settings
from backend.nl2sql.sql_generator import SQLGenerator
from backend.security.query_guard import QueryGuard


class Diagnostician(Protocol):
    async def diagnose(self, *, question: str, failed_sql: str, error_message: str, context: MetadataContext) -> SqlRepairProposal: ...


class SqlReviewer(Protocol):
    async def review(self, *, question: str, failed_sql: str, proposal: SqlRepairProposal, context: MetadataContext): ...


def validate_sql_scope(sql: str, *, dialect: str, context: MetadataContext) -> tuple[bool, str | None]:
    """Ensure SQL cannot access objects or base columns outside the approved closure."""
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.ParseError:
        return False, "SQL_SCOPE_PARSE_FAILED"
    allowed_tables = {name.lower() for name in context.schema_closure_object_ids}
    cte_names = {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE) if cte.alias_or_name}
    table_aliases: dict[str, str] = {}
    for table in parsed.find_all(exp.Table):
        table_name = table.name.lower()
        if table_name in cte_names:
            continue
        if table_name not in allowed_tables:
            return False, "TABLE_OUTSIDE_SCHEMA_CLOSURE"
        alias = table.alias_or_name.lower() if table.alias_or_name else table_name
        table_aliases[alias] = table_name

    allowed_fields = {
        (str(table.get("name")).lower(), str(column.get("name")).lower())
        for table in context.tables
        for column in table.get("columns", [])
        if table.get("name") and column.get("name")
    }
    allowed_field_names = {column for _, column in allowed_fields}
    select_aliases = {alias.alias.lower() for alias in parsed.find_all(exp.Alias) if alias.alias}
    for column in parsed.find_all(exp.Column):
        name = column.name.lower()
        qualifier = column.table.lower() if column.table else ""
        if qualifier in cte_names or name in select_aliases:
            continue
        if qualifier:
            table_name = table_aliases.get(qualifier, qualifier)
            if (table_name, name) not in allowed_fields:
                return False, "FIELD_OUTSIDE_SCHEMA_CLOSURE"
        elif name not in allowed_field_names:
            return False, "FIELD_OUTSIDE_SCHEMA_CLOSURE"
    return True, None


def validate_sql_month_scope(question: str, sql: str) -> tuple[bool, str | None]:
    """Require an explicit half-open range for every Chinese YYYY-year/month request."""
    requested_months = _requested_calendar_months(question)
    if not requested_months:
        return True, None
    for year, month in sorted(requested_months):
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        month_start = f"{year:04d}-{month:02d}-01"
        next_month_start = f"{next_year:04d}-{next_month:02d}-01"
        lower_bound = re.search(rf">=\s*(?:DATE\s*)?['\"]{re.escape(month_start)}(?:\s+00:00:00)?['\"]", sql, flags=re.IGNORECASE)
        upper_bound = re.search(rf"<\s*(?:DATE\s*)?['\"]{re.escape(next_month_start)}(?:\s+00:00:00)?['\"]", sql, flags=re.IGNORECASE)
        if not lower_bound or not upper_bound:
            return False, f"MONTH_TIME_RANGE_REQUIRED:{year:04d}-{month:02d}"
    return True, None


def _requested_calendar_months(question: str) -> set[tuple[int, int]]:
    return {
        (int(match.group("year")), int(match.group("month")))
        for match in re.finditer(r"(?P<year>20\d{2})\s*\u5e74\s*(?P<month>0?[1-9]|1[0-2])\s*\u6708", question)
    }


def repair_sql_month_scope(question: str, sql: str, *, dialect: str) -> str | None:
    """Deterministically correct one unambiguous calendar-month upper bound.

    The repair is intentionally narrow: the question must contain exactly one
    Chinese calendar month, the SQL must already contain that month's correct
    inclusive lower bound, and exactly one strict upper bound must target the
    same column.  Ambiguous SQL is left to the existing reviewed repair path.
    The returned SQL is still untrusted and must pass every normal guard again.
    """
    requested_months = _requested_calendar_months(question)
    if len(requested_months) != 1:
        return None
    year, month = next(iter(requested_months))
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    month_start = f"{year:04d}-{month:02d}-01"
    next_month_start = f"{next_year:04d}-{next_month:02d}-01"

    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.ParseError:
        return None

    def literal_date(node: exp.Expression) -> str | None:
        if isinstance(node, exp.Literal) and node.is_string:
            return str(node.this).split(" ", 1)[0]
        if isinstance(node, exp.Cast):
            return literal_date(node.this)
        return None

    lower_columns = {
        comparison.this.sql(dialect=dialect).lower()
        for comparison in parsed.find_all(exp.GTE)
        if literal_date(comparison.expression) == month_start
    }
    if len(lower_columns) != 1:
        return None
    target_column = next(iter(lower_columns))
    upper_bounds = [
        comparison
        for comparison in parsed.find_all(exp.LT)
        if comparison.this.sql(dialect=dialect).lower() == target_column
        and literal_date(comparison.expression) is not None
    ]
    if len(upper_bounds) != 1:
        return None

    upper_bound = upper_bounds[0]
    original_value = str(upper_bound.expression.this) if isinstance(upper_bound.expression, exp.Literal) else ""
    replacement = f"{next_month_start} 00:00:00" if " " in original_value else next_month_start
    upper_bound.set("expression", exp.Literal.string(replacement))
    repaired_sql = parsed.sql(dialect=dialect)
    valid, _ = validate_sql_month_scope(question, repaired_sql)
    return repaired_sql if valid else None


def validate_sql_latest_price_scope(question: str, sql: str, *, source_id: str) -> tuple[bool, str | None]:
    """Prevent a "latest" stock query from silently becoming a historical query.

    ``v_stock_latest_price`` already represents the latest available quote.
    A generated predicate such as ``trade_date < '2024-01-01'`` therefore
    contradicts a question asking for "最新" and commonly returns stale or empty
    data.  This is a semantic safety check, not a substitute for SQL scope
    validation: a repair still has to pass QueryGuard and the schema closure.
    """
    if source_id != "postgres_stock" or "最新" not in question:
        return True, None
    if "v_stock_latest_price" not in sql.lower():
        return True, None
    # A comparison period in a *different* history-table subquery is valid
    # (for example, latest price versus July's average).  Only reject a fixed
    # date predicate applied to the latest-price view itself.
    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
    except sqlglot.errors.ParseError:
        # QueryGuard will report the parse failure with its normal safe path.
        return True, None
    latest_tables = [
        table
        for table in parsed.find_all(exp.Table)
        if table.name.lower() == "v_stock_latest_price"
    ]
    latest_aliases = {
        table.alias.lower()
        for table in latest_tables
        if table.args.get("alias") and table.alias
    }
    alias_predicate = any(
        re.search(
            rf"\b{re.escape(alias)}\.trade_date\s*(?:=|>=|<=|<|>)\s*(?:DATE\s*)?['\"]20\d{{2}}-\d{{2}}-\d{{2}}['\"]",
            sql,
            flags=re.IGNORECASE,
        )
        for alias in latest_aliases
    )
    unaliased_latest_view = any(not table.args.get("alias") for table in latest_tables)
    unaliased_predicate = unaliased_latest_view and bool(
        re.search(
            r"(?<!\.)\btrade_date\s*(?:=|>=|<=|<|>)\s*(?:DATE\s*)?['\"]20\d{2}-\d{2}-\d{2}['\"]",
            sql,
            flags=re.IGNORECASE,
        )
    )
    if alias_predicate or unaliased_predicate:
        return False, "LATEST_PRICE_MUST_NOT_FILTER_HISTORICAL_DATE"
    return True, None


class ControlledSingleSourceExecutor:
    """Runs only an approved, single-source XiYan plan with at most one repair."""

    def __init__(
        self,
        *,
        sql_generator: SQLGenerator | None = None,
        diagnostician: Diagnostician | None = None,
        reviewer: SqlReviewer | None = None,
        adapter_provider=get_adapter,
    ) -> None:
        self.sql_generator = sql_generator or SQLGenerator()
        self.diagnostician = diagnostician or QwenSqlDiagnostician()
        self.reviewer = reviewer or QwenSqlReviewer()
        self.adapter_provider = adapter_provider

    async def execute(self, question: str, plan: AgentPlan, contexts: list[MetadataContext]) -> AgentExecutionResult:
        """
        在 LangGraph 工作流里，这是 "execute" 节点的执行函数。
        上游：validate（策略校验）→ review（Qwen 复审）通过后才会被调用。
        下游：summarize 节点把结果转成自然语言答案。
        本函数只负责"组装输入 + 生成 SQL + 移交安全闸门"，真正的执行/校验在 _attempt_execution 里。
        """
        # 第 1 关：plan 必须是 single_source 且只能有 1 个子任务
        # 原因：多源融合在当前 executor 里没实现，硬执行容易出"半截拼接"的事故
        if plan.route_mode != "single_source" or len(plan.subtasks) != 1:
            return AgentExecutionResult(success=False, error="ONLY_SINGLE_SOURCE_PLAN_SUPPORTED")
        task = plan.subtasks[0]

        # 第 2 关：从已批准的 contexts 里找回 task.source_id 对应的元数据
        # 防止 plan 引用的 source_id 跟 retrieve 阶段批准的列表对不上
        # （比如 retrieve 时是 postgres_stock，plan 却被改成了别的）
        context = next((item for item in contexts if item.source.source_id == task.source_id), None)
        if context is None:
            return AgentExecutionResult(success=False, error="APPROVED_CONTEXT_NOT_FOUND")

        # 第 3 关：只允许 readonly_sql；rest_get / graphql_query 暂不实装
        # 注意：contracts.AgentSubtask.operation_id 字面上是允许三种的（Literal 类型），
        # 那是给将来扩展用的"接口预留"，执行层要在这里再卡一道
        if task.operation_id != "readonly_sql":
            return AgentExecutionResult(success=False, error="ONLY_READONLY_SQL_SUPPORTED")

        # 第 4 关：组装 XiYan prompt 上下文，喂给 SQL 生成器
        # 这一坨字段就是"喂给 XiYan 模型的所有东西"——越界越少，模型越不可能胡写
        prompt_context = XiYanPromptContext(
            source_id=context.source.source_id,                          # 数据源 ID（决定走哪个 adapter）
            dialect=context.source.dialect,                              # SQL 方言（postgres / mysql / hive …）
            schema_signature=context.schema_signature,                   # Schema 指纹，用来命中 prompt 缓存
            question=question,                                           # 用户原问题
            task_goal=task.goal,                                         # 规划器提炼出的"子任务目标"
            required_object_ids=task.object_ids,                         # 规划器声明要用到的表（白名单）
            planned_output_fields=task.output_fields,                    # 规划器声明的输出字段（参考白名单）
            schema_closure_object_ids=context.schema_closure_object_ids, # retrieve 阶段补全后的表集合（白名单）
            allowed_field_ids=sorted(                                    # 表名.列名 形式的允许字段列表（白名单）
                f"{table.get('name')}.{column.get('name')}"
                for table in context.tables
                for column in table.get("columns", [])
                if table.get("name") and column.get("name")
            ),
            max_rows=settings.max_rows_return,                           # LIMIT 上限，防止大结果集打爆
        )

        # metadata 是生成器内部可能用到的额外信息（schema 缓存键、表详情）
        metadata = {"schema_signature": context.schema_signature, "tables": context.tables}

        # Preserve the complete server-built prompt in the trace.  It is not sent
        # to the browser and contains no credential or raw result row.
        sql, _, generation_error, generation_trace = await self.sql_generator.generate_controlled_sql(
            prompt_context,
            metadata,
            model_id=settings.agent_sql_model_id,
        )
        # 生成阶段就失败（模型崩、JSON 不合规、超时…）就直接返回，不再走后面闸门
        if generation_error:
            return AgentExecutionResult(success=False, error=generation_error, generation_trace=generation_trace)

        # 把生成的 SQL 交给 _attempt_execution，进入"纵深防御"：
        # 1) QueryGuard 只读白名单      2) validate_sql_scope 字段/表范围
        # 3) 月份语义校验               4) 最新价语义校验
        # 任意一道不过 → _repair_or_fail 走一次 Qwen 修复，再不行就 fail
        result = await self._attempt_execution(question, sql, context, retry_attempted=False)
        result.generation_trace = generation_trace
        return result

    async def _attempt_execution(
        self,
        question: str,
        sql: str,
        context: MetadataContext,
        *,
        retry_attempted: bool,
    ) -> AgentExecutionResult:
        is_safe, error = QueryGuard.validate_read_only(sql, context.source.dialect)
        if not is_safe:
            return await self._repair_or_fail(question, sql, error or "QUERY_GUARD_BLOCKED", context, retry_attempted)
        in_scope, scope_error = validate_sql_scope(sql, dialect=context.source.dialect, context=context)
        if not in_scope:
            return await self._repair_or_fail(question, sql, scope_error or "SQL_SCOPE_BLOCKED", context, retry_attempted)
        month_in_scope, month_scope_error = validate_sql_month_scope(question, sql)
        if not month_in_scope:
            deterministic_sql = repair_sql_month_scope(question, sql, dialect=context.source.dialect)
            if deterministic_sql and not retry_attempted:
                result = await self._attempt_execution(
                    question, deterministic_sql, context, retry_attempted=True
                )
                result.repair_trace = {
                    "strategy": "deterministic_calendar_month",
                    "trigger": month_scope_error or "MONTH_TIME_RANGE_REQUIRED",
                    "status": "applied" if result.success else "revalidated_failed",
                    "final_error": result.error,
                }
                return result
            return await self._repair_or_fail(question, sql, month_scope_error or "MONTH_TIME_RANGE_REQUIRED", context, retry_attempted)
        latest_in_scope, latest_scope_error = validate_sql_latest_price_scope(
            question, sql, source_id=context.source.source_id
        )
        if not latest_in_scope:
            return await self._repair_or_fail(
                question, sql, latest_scope_error or "LATEST_PRICE_MUST_NOT_FILTER_HISTORICAL_DATE", context, retry_attempted
            )

        safe_sql = QueryGuard.sanitize_for_execution(sql, context.source.dialect, settings.max_rows_return)
        adapter = self.adapter_provider(context.source.source_id)
        try:
            results, columns = await asyncio.wait_for(
                asyncio.to_thread(adapter.execute_query, safe_sql), timeout=settings.agent_execute_timeout
            )
        except Exception as exc:
            return await self._repair_or_fail(question, sql, f"EXECUTION_FAILED:{type(exc).__name__}", context, retry_attempted)
        return AgentExecutionResult(
            success=True,
            sql=safe_sql,
            columns=columns,
            results=results,
            row_count=len(results),
            retry_attempted=retry_attempted,
        )

    async def _repair_or_fail(
        self,
        question: str,
        failed_sql: str,
        error_message: str,
        context: MetadataContext,
        retry_attempted: bool,
    ) -> AgentExecutionResult:
        if retry_attempted:
            return AgentExecutionResult(success=False, sql=failed_sql, retry_attempted=True, error=error_message)
        try:
            proposal = await self.diagnostician.diagnose(
                question=question, failed_sql=failed_sql, error_message=error_message, context=context
            )
            if not proposal.can_retry or proposal.risk != "low" or not proposal.proposed_sql.strip():
                return AgentExecutionResult(success=False, sql=failed_sql, retry_attempted=True, error="REPAIR_NOT_ALLOWED")
            decision = await self.reviewer.review(
                question=question, failed_sql=failed_sql, proposal=proposal, context=context
            )
            if not decision.approve:
                return AgentExecutionResult(success=False, sql=failed_sql, retry_attempted=True, error="REPAIR_REJECTED")
        except AgentLLMError:
            return AgentExecutionResult(success=False, sql=failed_sql, retry_attempted=True, error="REPAIR_REVIEW_FAILED")
        return await self._attempt_execution(question, proposal.proposed_sql, context, retry_attempted=True)
