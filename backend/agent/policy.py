"""
策略引擎会拒绝未授权数据源、越界字段/对象、错误操作类型、
循环依赖和不支持的融合契约；多源缺少融合契约时只返回一次“需要修订”。
AgentPlan + MetadataContext → 验证 → PlanValidationResult
    ↓
1. 检查任务 ID 是否重复
2. 检查路由模式是否正确
3. 检查数据源是否授权
4. 检查操作类型是否匹配
5. 检查表/字段是否超出权限
6. 检查依赖关系是否有误
7. 检查是否有循环依赖
8. 检查多源融合契约是否合规
"""
from __future__ import annotations

from collections import defaultdict
import re

from backend.agent.contracts import AgentPlan, MetadataContext, PlanValidationResult

_SQL_SOURCE_TYPES = {"sqlite", "mysql", "postgresql", "gauss", "hive", "dameng", "rest_api", "graphql"}
_API_SOURCE_TYPES = {"rest_api", "graphql"}
# 支持的多源融合契约（JOIN 策略）
_SUPPORTED_MERGE_CONTRACTS = {"city_key_left_join", "date_key_left_join", "code_key_left_join"}


def validate_plan(
        plan: AgentPlan,
        contexts: list[MetadataContext],
        *,
        supported_merge_contracts: set[str] | None = None,
) -> PlanValidationResult:
    """
    主验证函数
    :param plan: Agent 提交的执行计划
    :param contexts: 各数据源的元数据上下文
    :param supported_merge_contracts: 自定义支持的融合契约（可选）
    :return:
        1	任务 ID 重复	❌ 拒绝	DUPLICATE_TASK_ID
        2	单源模式任务数量	❌ 拒绝	SINGLE_SOURCE_TASK_COUNT
        3	数据源授权	❌ 拒绝	UNAUTHORIZED_SOURCE:xxx
        4	操作类型匹配	❌ 拒绝	OPERATION_NOT_ALLOWED:task_1
    """
    # 把 contexts 列表转成字典，方便通过 source_id 快速查找。
    context_by_source = {context.source.source_id: context for context in contexts}
    errors: list[str] = []
    revision_needed: list[str] = []
    # plan = AgentPlan(
    #     route_mode="single_source",
    #     subtasks=[
    #         AgentSubtask(id="task_1", ...),
    #         AgentSubtask(id="task_2", ...),
    #         AgentSubtask(id="task_3", ...),
    #     ]
    # )
    # 结果: ["task_1", "task_2", "task_3"]
    task_ids = [task.id for task in plan.subtasks]

    # 验证任务 ID 和路由模式
    # task_ids = ["task_1", "task_2", "task_1"]
    # set(task_ids) = {"task_1", "task_2"}
    # len(task_ids) = 3, len(set(task_ids)) = 2
    # 不相等 → ID 重复
    # 作用：防止 Agent 生成重复的任务 ID。
    if len(task_ids) != len(set(task_ids)):
        errors.append("DUPLICATE_TASK_ID")  # 任务 ID 重复
    # 单源模式下任务数不为 1
    # 单源模式设计上就不支持多任务
    if plan.route_mode == "single_source" and len(plan.subtasks) != 1:
        errors.append("SINGLE_SOURCE_TASK_COUNT")
    #  merge_contract_id 用于多源融合（如 LEFT JOIN）
    # 单源模式只有一个数据源，不需要融合
    # 单源模式不允许融合契约
    if plan.route_mode == "single_source" and plan.merge_contract_id:
        revision_needed.append("SINGLE_SOURCE_MERGE_FORBIDDEN")

    for task in plan.subtasks:
        # 如果 task.source_id 不在 contexts 中，说明该数据源未授权。
        context = context_by_source.get(task.source_id)
        if context is None:
            errors.append(f"UNAUTHORIZED_SOURCE:{task.source_id}")
            continue

        source_type = context.source.source_type
        if task.operation_id == "readonly_sql" and source_type not in _SQL_SOURCE_TYPES:
            errors.append(f"OPERATION_NOT_ALLOWED:{task.id}")
        if task.operation_id == "rest_get" and source_type != "rest_api":
            errors.append(f"OPERATION_NOT_ALLOWED:{task.id}")
        if task.operation_id == "graphql_query" and source_type != "graphql":
            errors.append(f"OPERATION_NOT_ALLOWED:{task.id}")

        available_objects = set(context.schema_closure_object_ids)
        unknown_objects = sorted(set(task.object_ids) - available_objects)
        if unknown_objects:
            revision_needed.append(f"OBJECT_OUTSIDE_SCHEMA_CLOSURE:{task.id}")

        available_fields = {
            f"{table.get('name')}.{column.get('name')}"
            for table in context.tables
            for column in table.get("columns", [])
            if table.get("name") and column.get("name")
        }
        # output_fields can be direct fields, aliases, or a small aggregate
        # expression such as COUNT(alerts.alert_no).  Validate every qualified
        # reference inside an expression, rather than comparing the entire SQL
        # fragment with a physical field name.  Generated SQL is still checked
        # by QueryGuard and validate_sql_scope before execution.
        unknown_fields = sorted({
            reference
            for output_field in task.output_fields
            for reference in _qualified_field_references(output_field)
            if reference not in available_fields
            and not _is_valid_self_join_alias_reference(reference, task.object_ids, context.tables, available_fields)
        })
        if unknown_fields:
            revision_needed.append(f"FIELD_OUTSIDE_SCHEMA_CLOSURE:{task.id}")

        for dependency in task.depends_on:
            if dependency not in task_ids:
                errors.append(f"UNKNOWN_DEPENDENCY:{task.id}")
            elif dependency == task.id:
                errors.append(f"SELF_DEPENDENCY:{task.id}")

    if _has_dependency_cycle(plan):
        errors.append("DEPENDENCY_CYCLE")

    allowed_merges = supported_merge_contracts or _SUPPORTED_MERGE_CONTRACTS
    if plan.route_mode == "multi_source":
        if not plan.merge_contract_id:
            revision_needed.append("MISSING_MERGE_CONTRACT")
        elif plan.merge_contract_id not in allowed_merges:
            errors.append("UNSUPPORTED_MERGE_CONTRACT")

    if errors:
        return PlanValidationResult(status="rejected", reason_codes=sorted(set(errors)))
    if revision_needed:
        return PlanValidationResult(status="revise", reason_codes=revision_needed)
    return PlanValidationResult(status="approved")


def _has_dependency_cycle(plan: AgentPlan) -> bool:
    graph: dict[str, list[str]] = defaultdict(list)
    for task in plan.subtasks:
        graph[task.id].extend(task.depends_on)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        if any(visit(dependency) for dependency in graph[task_id]):
            return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task.id) for task in plan.subtasks)


def _qualified_field_references(output_field: str) -> set[str]:
    """Return table.field references from an untrusted plan display expression."""
    return {
        f"{table}.{field}"
        for table, field in re.findall(r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\b", output_field)
    }


def _is_valid_self_join_alias_reference(
    reference: str,
    object_ids: list[str],
    tables: list[dict],
    available_fields: set[str],
) -> bool:
    """Allow ``manager.name``-style aliases for one selected self-joined table.

    The plan names physical objects, while a self join necessarily introduces a
    second SQL alias.  Accept that alias only when the selected physical table
    has a verified foreign key back to itself and owns the referenced column.
    SQL scope validation still checks the generated SQL aliases before execute.
    """
    if "." not in reference or len(set(object_ids)) != 1:
        return False
    _alias, column = reference.split(".", 1)
    table_name = object_ids[0]
    if f"{table_name}.{column}" not in available_fields:
        return False
    table = next((item for item in tables if item.get("name") == table_name), None)
    if not table:
        return False
    return any(
        foreign_key.get("ref_table") == table_name
        and foreign_key.get("column")
        and foreign_key.get("ref_column")
        for foreign_key in (table.get("foreign_keys") or [])
    )
