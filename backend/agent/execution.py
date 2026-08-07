"""Guarded single-source execution for plans approved by the controlled graph."""
from __future__ import annotations

import asyncio
import re
from typing import Protocol

import sqlglot
from sqlglot import exp

from backend.adapters.registry import get_adapter
from backend.agent.contracts import AgentExecutionResult, AgentPlan, MetadataContext, SqlRepairProposal, XiYanPromptContext
from backend.agent.llm import AgentLLMError, DeepSeekSqlReviewer, QwenSqlDiagnostician
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
    requested_months = {
        (int(match.group("year")), int(match.group("month")))
        for match in re.finditer(r"(?P<year>20\d{2})\s*\u5e74\s*(?P<month>0?[1-9]|1[0-2])\s*\u6708", question)
    }
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
        self.reviewer = reviewer or DeepSeekSqlReviewer()
        self.adapter_provider = adapter_provider

    async def execute(self, question: str, plan: AgentPlan, contexts: list[MetadataContext]) -> AgentExecutionResult:
        if plan.route_mode != "single_source" or len(plan.subtasks) != 1:
            return AgentExecutionResult(success=False, error="ONLY_SINGLE_SOURCE_PLAN_SUPPORTED")
        task = plan.subtasks[0]
        context = next((item for item in contexts if item.source.source_id == task.source_id), None)
        if context is None:
            return AgentExecutionResult(success=False, error="APPROVED_CONTEXT_NOT_FOUND")
        if task.operation_id != "readonly_sql":
            return AgentExecutionResult(success=False, error="ONLY_READONLY_SQL_SUPPORTED")

        prompt_context = XiYanPromptContext(
            source_id=context.source.source_id,
            dialect=context.source.dialect,
            schema_signature=context.schema_signature,
            question=question,
            task_goal=task.goal,
            required_object_ids=task.object_ids,
            planned_output_fields=task.output_fields,
            schema_closure_object_ids=context.schema_closure_object_ids,
            allowed_field_ids=sorted(
                f"{table.get('name')}.{column.get('name')}"
                for table in context.tables
                for column in table.get("columns", [])
                if table.get("name") and column.get("name")
            ),
            max_rows=settings.max_rows_return,
        )
        metadata = {"schema_signature": context.schema_signature, "tables": context.tables}
        sql, _, generation_error, _ = await self.sql_generator.generate_controlled_sql(
            prompt_context,
            metadata,
            model_id=settings.agent_sql_model_id,
        )
        if generation_error:
            return AgentExecutionResult(success=False, error=generation_error)
        return await self._attempt_execution(question, sql, context, retry_attempted=False)

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
            return await self._repair_or_fail(question, sql, month_scope_error or "MONTH_TIME_RANGE_REQUIRED", context, retry_attempted)

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
