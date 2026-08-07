"""
为 NL2SQL 提示构建一个有界、关系完备的模式上下文。

表选择器回答“哪些对象看起来相关”。此模块回答

“要使这些对象可用，还需要哪些其他对象”。

它有意仅操作服务器提供的元数据和目录数据。

在现有表相关性排序后补齐外键、反向关联和 Catalog 逻辑外键，最多保留 5 个对象。
作用是避免 Prompt 中缺少 products 这类 JOIN 必需表。
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any, Iterable

from backend.nl2sql.catalog import get_source_catalog


def expand_schema_closure(
    metadata: dict[str, Any],
    selected_objects: Iterable[str],
    *,
    data_source: str = "",
    max_objects: int = 5,
) -> list[str]:
    """
    在表选择的基础上，自动补全关联表，确保生成的 SQL 有足够信息。

    :param metadata: 提供表之间的关系信息（外键、依赖）
    :param selected_objects: 用户初步选的表
    :param data_source: 用于获取 catalog 中的逻辑外键
    :param max_objects: 限制表数量，控制 Prompt 长度
    :return:
    """

    # 例如 输入：
    # metadata = {
    #     "tables": [
    #         {"name": "orders", "foreign_keys": [{"ref_table": "customers"}]},
    #         {"name": "customers", "columns": [...]},
    #         {"name": "products", "foreign_keys": [{"ref_table": "categories"}]},
    #         {"name": "categories", "columns": [...]},
    #     ]
    # }
    #
    # selected_objects = ["orders"]  # 用户只选了 orders
    # max_objects = 5
    # 输出：["orders", "customers"]  # 自动补全了 customers
    if max_objects <= 0:
        return []

    tables = metadata.get("tables", []) or []

    # 构建一个"小写表名 → 原始表名"的映射，用于不区分大小写的查找
    names_by_lower = {
        str(table.get("name", "")).lower(): str(table.get("name", ""))
        for table in tables
        if table.get("name")
    }

    if not names_by_lower:
        return []

    selected: list[str] = []
    for name in selected_objects:
        actual = names_by_lower.get(str(name).lower())
        if actual and actual not in selected:
            selected.append(actual)
        if len(selected) >= max_objects:
            return selected

    if not selected:
        return []

    neighbours = _build_neighbour_map(tables, names_by_lower, data_source)
    # print(f"DEBUG: initial selected={selected}, neighbours={dict(neighbours)}")
    # 广度优先搜索（BFS, Breadth-First Search） 的实现，用于从已选表出发，逐层查找关联的表。
    # 初始化队列：把已选的表放进队列，作为 BFS 的起始点。
    queue: deque[str] = deque(selected)
    # queue 不为空（还有表需要处理）
    # selected 没达到上限（默认 5 个）
    while queue and len(selected) < max_objects:
        # 取出首元素并从队列中移除
        current = queue.popleft()
        for neighbour in neighbours.get(current, []):
            # 跳过已选过的表：避免重复处理。
            if neighbour in selected:
                continue
            #     selected：记录已找到的表
            # queue：加入队列，后续处理它的邻居
            selected.append(neighbour)
            queue.append(neighbour)
            if len(selected) >= max_objects:
                break

    # print(f"DEBUG: final selected={selected}, neighbours={dict(neighbours)}")
    return selected


def _build_neighbour_map(
    tables: list[dict[str, Any]],
    names_by_lower: dict[str, str],
    data_source: str,
) -> dict[str, list[str]]:
    """
    构建表之间的邻居关系联图，即"哪些表和其他表相关"。
    :param tables:
    :param names_by_lower:
    :param data_source:
    :return:
    {
    # 双向添加
    "orders": ["customers", "products"],   # orders 和 customers、products 相关
    "customers": ["orders"],               # customers 只和 orders 相关
    "products": ["orders", "categories"],
    "categories": ["products"],
    }
    """
    neighbours: dict[str, list[str]] = {name: [] for name in names_by_lower.values()}

    def actual_name(value: Any) -> str | None:
        return names_by_lower.get(str(value or "").lower())

    def connect(left: Any, right: Any) -> None:
        left_name, right_name = actual_name(left), actual_name(right)
        if not left_name or not right_name or left_name == right_name:
            return
        # 双向连接：left 的邻居包含 right，right 的邻居包含 left
        if right_name not in neighbours[left_name]:
            neighbours[left_name].append(right_name)
        if left_name not in neighbours[right_name]:
            neighbours[right_name].append(left_name)

    # 如果 table 是 {"name": "orders", "foreign_keys": [{"ref_table": "customers"}]}
    # → connect("orders", "customers")
    # → "orders" 的邻居加 "customers"
    # → "customers" 的邻居加 "orders"
    for table in tables:
        table_name = table.get("name")
        for fk in table.get("foreign_keys", []) or []:
            if isinstance(fk, dict):
                connect(table_name, fk.get("ref_table"))
    # 当前项目的适配器 metadata 契约没有 depends_on/dependencies/
    # base_tables/source_tables 字段；视图依赖只能来自显式 Catalog 规则。
    # 5. 处理 Catalog 逻辑外键  帮助选择相关表和构建 Prompt
    #     "logical_foreign_keys": [
    #         "hadoop_order_events.user_id -> hadoop_user_profiles.user_id",
    #         "hadoop_order_events.product_id -> hadoop_product_dim.product_id",
    #         "hadoop_order_events.region_id -> hadoop_region_dim.region_id",
    #         "hadoop_user_profiles.region_id -> hadoop_region_dim.region_id",
    #     ]
    for relation in get_source_catalog(data_source).get("logical_foreign_keys", []) or []:
        # "hadoop_order_events.user_id -> hadoop_user_profiles.user_id"
        left, right = _logical_fk_tables(relation)
        # hadoop_order_events  hadoop_user_profiles
        connect(left, right)
    return neighbours


def _logical_fk_tables(relation: Any) -> tuple[str | None, str | None]:
    """
    从各种格式中提取两个表名，用于构建逻辑外键关系。
    :param relation:
    :return:
    """
    # 1. 字典格式处理
    # relation = {"table": "orders", "ref_table": "customers"}
    # # 返回: ("orders", "customers")
    #
    # relation = {"from_table": "users", "to_table": "profiles"}
    # # 返回: ("users", "profiles")
    #
    # relation = {"left_table": "A", "right_table": "B"}
    # # 返回: ("A", "B")

    if isinstance(relation, dict):
        return (
            relation.get("table") or relation.get("from_table") or relation.get("left_table"),
            relation.get("ref_table") or relation.get("to_table") or relation.get("right_table"),
        )
    # 2. 字符串格式处理
    # relation = "orders.customer_id = customers.id"
    # # 通过正则提取: ("orders", "customers")
    #
    # relation = "users.id -> profiles.user_id"
    # # 提取: ("users", "profiles")
    text = str(relation or "")
    # 正则匹配: table1.column1 = table2.column2
    matches = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*", text)
    # 返回两个表名
    return (matches[0], matches[1]) if len(matches) >= 2 else (None, None)
