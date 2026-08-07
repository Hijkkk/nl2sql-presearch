"""
服务器控制的发现和元数据检索工具。
这些工具是刻意设计的确定性工具。规划模型可以使用它们的
输出，但无法选择未列出的来源或通过
此模块获取凭据。

实现 discover_sources 和 retrieve_metadata_context：只从已启用的数据源中自动选候选源，
再检索关系完整的 Schema；不暴露连接串、密码或真实业务数据。
"""
from __future__ import annotations

from typing import Callable, Iterable

from backend.adapters.registry import get_adapter
from backend.agent.contracts import MetadataContext, SourceCandidate, SourceDescriptor
from backend.config.config import settings
from backend.nl2sql.catalog import get_source_catalog
from backend.nl2sql.schema_context import expand_schema_closure
from backend.nl2sql.table_selector import extract_chinese_bigrams, select_relevant_tables, tokenize_question
from backend.agent.vector_store import create_metadata_store


# These are routing-only business terms, not schema or data.  They make an
# explicit domain mention a stronger signal than an otherwise generic vector
# match, and keep the fallback behaviour inspectable in Agent traces.
SOURCE_ROUTING_TERMS: dict[str, tuple[str, ...]] = {
    "sqlite_demo": ("员工", "部门", "销售", "薪资"),
    "mysql_police_address": ("警情", "报警", "治安", "案件", "结案", "案发", "辖区", "地址", "人员"),
    "postgres_stock": ("股票", "证券", "行情", "基金", "etf", "行业", "股价"),
    "gauss_ecommerce": ("电商", "订单", "商品", "客户", "购买", "销量"),
    "hive_hadoop_demo": ("hadoop", "hive", "日志", "大数据", "分布式"),
    "dameng_ecommerce": ("达梦", "电商", "订单", "商品", "客户", "销量"),
    "rest_api_demo": ("天气", "气温", "湿度", "预报", "城市天气"),
    "countries_graphql": ("国家", "洲", "货币", "语言", "首都"),
}


def _required_schema_seeds(question: str, source_id: str, available_objects: set[str]) -> list[str]:
    """Return small, intent-critical schema seeds before semantic reranking.

    These are not a permission bypass: every returned object must exist in the
    adapter metadata and remains subject to the normal bounded schema closure
    and plan/SQL policy checks.  They protect relationship tables whose
    relevance is explicit in the question from being displaced by a generic
    vector hit when the prompt object budget is small.
    """
    question_lower = question.lower()
    seeds: list[str] = []
    if source_id in {"mysql_police_address", "police_address"}:
        # Unicode escapes keep this server-side matching stable even if a
        # Windows editor or shell has a non-UTF-8 code page.
        asks_alert = any(term in question_lower for term in ("\u62a5\u8b66", "\u8b66\u60c5", "\u63a5\u8b66", "\u6848\u53d1", "\u6cbb\u5b89"))
        asks_involved_person = any(
            term in question_lower
            for term in ("\u5acc\u7591", "\u5acc\u7591\u4eba", "\u6d89\u6848", "\u6d89\u53ca", "\u53d7\u5bb3", "\u62a5\u8b66\u4eba", "\u8bc1\u4eba")
        )
        if asks_alert and asks_involved_person:
            # An alert/person-role question needs the alert record, its
            # involvement bridge, and the role dictionary as one unit.
            seeds.extend(("police_alert", "alert_involvement", "dict_alert_role"))
    return [name for name in dict.fromkeys(seeds) if name in available_objects]


def configured_source_descriptors() -> list[SourceDescriptor]:
    """
    返回已启用的源描述，但不暴露连接设置。用于数据源的选择
    :return:
    """
    descriptors = [
        SourceDescriptor(
            source_id="sqlite_demo",
            source_type="sqlite",
            dialect="sqlite",
            description="内置演示数据库，包含员工、部门和销售数据。",
            # 只读 聚合 链接
            capabilities=["readonly_sql", "aggregation", "join"],
        )
    ]
    configured_sources = (
        # 是否开启查询、查询名、数据源、sql类型、描述
        (settings.mysql_query_enabled, settings.mysql_query_name, "mysql", "mysql", settings.mysql_query_description),
        (settings.postgres_query_enabled, settings.postgres_query_name, "postgresql", "postgres", settings.postgres_query_description),
        (settings.gauss_query_enabled, settings.gauss_query_name, "gauss", "postgres", settings.gauss_query_description),
        (settings.hive_query_enabled, settings.hive_query_name, "hive", "hive", settings.hive_query_description),
        # Dameng uses Oracle-compatible SQL in the adapter and QueryGuard.
        (settings.dameng_query_enabled, settings.dameng_query_name, "dameng", "oracle", settings.dameng_query_description),
        # Both adapters expose fixed remote responses as read-only SQLite
        # virtual tables.  The planner never receives a URL or free-form API
        # request capability; it generates SQL only against that bounded view.
        (settings.rest_api_enabled, settings.rest_api_name, "rest_api", "sqlite", settings.rest_api_description),
        (settings.graphql_enabled, settings.graphql_name, "graphql", "sqlite", settings.graphql_description),
    )
    for enabled, source_id, source_type, dialect, description in configured_sources:
        if enabled:
            descriptors.append(
                SourceDescriptor(
                    source_id=source_id,
                    source_type=source_type,
                    dialect=dialect,
                    description=description,
                    capabilities=["readonly_sql"] if source_type not in {"rest_api", "graphql"} else ["read_operation"],
                )
            )
    return descriptors


def discover_sources(
    question: str,
    *,
    allowed_sources: Iterable[SourceDescriptor] | None = None,
    limit: int = 3,
) -> list[SourceCandidate]:
    """
    仅对已授权的来源进行词法排名。
    这是确定性的 MVP 检索后端。其公共契约
    有意设计为便于日后用 Qdrant 替换评分实现
    而无需更改规划器或执行器代码。
    :param question: 用户问题
    :param allowed_sources: Iterable：可迭代对象（如 list, tuple, set, generator）一个包含 SourceDescriptor 对象的可迭代序列
    :param limit: 限制的数据源数量
    :return:
    """
    if limit <= 0:
        return []
    # 如果允许的源为空，则使用已配置的源
    candidates = list(configured_source_descriptors() if allowed_sources is None else allowed_sources)
    # 让关键词匹配不区分大小写，提高匹配准确率。
    terms = {term.lower() for term in tokenize_question(question)}
    # 问题转为小写
    question_lower = question.lower()
    question_bigrams = extract_chinese_bigrams(question_lower)
    # 储最终打分后的结果
    ranked: list[SourceCandidate] = []
    for descriptor in candidates:
        # descriptor 包含：
        # source_id：数据源 ID（如 "mysql_sales"）
        # source_type：数据源类型（如 "mysql"）
        # dialect：SQL 方言（如 "MySQL"）
        # description：数据源描述
        # capabilities：能力列表（如 ["query", "join"]）
        # Catalog carries source-specific business terms (for example police
        # alert synonyms) that are stronger evidence than a generic embedding
        # similarity.  It is metadata only and never contains credentials.
        catalog = get_source_catalog(descriptor.source_id)
        synonyms = catalog.get("synonyms") or {}
        catalog_terms = [str(item) for item in catalog.get("preferred_objects", []) or []]
        catalog_terms.extend(str(key) for key in synonyms)
        for values in synonyms.values():
            if isinstance(values, list):
                catalog_terms.extend(str(value) for value in values)
            else:
                catalog_terms.append(str(values))
        # 把数据源的所有可搜索字段拼成一个大字符串，转小写。
        searchable = " ".join(
            [
                descriptor.source_id,
                descriptor.source_type,
                descriptor.dialect,
                descriptor.description,
                *descriptor.capabilities,
                *SOURCE_ROUTING_TERMS.get(descriptor.source_id, ()),
                *catalog_terms,
            ]
        ).lower()
        # terms = {"销售", "订单", "产品"}
        # searchable = "mysql_sales mysql mysql 销售数据库，包含订单和产品信息 query join aggregation"
        # matched_terms = ["订单", "产品", "销售"]  # 三个词都匹配了
        matched_terms = sorted(term for term in terms if term in searchable)
        score = float(len(matched_terms) * 2)
        matched_bigrams = sorted(question_bigrams & extract_chinese_bigrams(searchable))
        score += float(min(len(matched_bigrams), 3))
        matched_terms.extend(bigram for bigram in matched_bigrams if bigram not in matched_terms)
        # 果问题中直接提到了数据源 ID，额外加 10 分。
        if descriptor.source_id.lower() in question_lower:
            score += 10.0
        descriptor.model_dump()
        # 输出:
        # {
        #     "source_id": "mysql_sales",
        #     "source_type": "mysql",
        #     "description": "销售数据库",
        #     "dialect": "MySQL",
        #     "capabilities": ["query", "join"]
        # }
        # ------------->
        # SourceCandidate(
        #     source_id=descriptor.source_id,
        #     source_type=descriptor.source_type,
        #     description=descriptor.description,
        #     dialect=descriptor.dialect,
        #     capabilities=descriptor.capabilities,
        #     score=score,
        #     matched_terms=matched_terms
        # )
        ranked.append(
            SourceCandidate(
                **descriptor.model_dump(),
                score=score,
                matched_terms=matched_terms,
                lexical_score=score,
                hybrid_score=score,
            )
        )
    # 排序规则：
    # -item.score：分数从高到低（负号实现降序）
    # item.source_id：分数相同时，按 source_id 字典序排序
    # 取前 limit 个。
    # Semantic scores are primary once the indexed collection is available.  The
    # lexical score remains a deterministic reranker and a no-network fallback.
    store = create_metadata_store()
    if store:
        try:
            semantic_hits = store.search(question, kind="source", limit=len(candidates))
            semantic_scores = {str(hit.get("source_id")): float(hit.get("vector_score", 0.0)) for hit in semantic_hits}
            if semantic_scores:
                for candidate in ranked:
                    semantic_score = semantic_scores.get(candidate.source_id)
                    if semantic_score is None:
                        continue
                    # Keep the full deterministic score.  Reducing it here made
                    # an unindexed source with the same generic business terms
                    # outrank an indexed source with an actual semantic match.
                    candidate.score = semantic_score * 10.0 + candidate.score
                    candidate.retrieval_method = "semantic_hybrid"
                    candidate.semantic_score = semantic_score
                    candidate.hybrid_score = candidate.score
        except Exception:
            # Service availability is not a reason to weaken the previous safe
            # retrieval path.  Observability is captured at graph level.
            pass
    return sorted(ranked, key=lambda item: (-item.score, item.source_id))[:limit]


def retrieve_metadata_context(
    question: str,
    source: SourceDescriptor,
    *,
    object_limit: int = 5,
    adapter_provider: Callable[[str], object] = get_adapter,
) -> MetadataContext:
    """
    元数据检索和构建函数，用于为指定数据源构建一个"有界、关系完备的模式上下文"。
    :param question:
    :param source:
    :param object_limit:
    :param adapter_provider:
    :return:
    """
    # 限制 object_limit 必须是正数。
    if object_limit <= 0:
        raise ValueError("object_limit must be positive")
    # adapter_provider 默认是 get_adapter
    # 通过 source.source_id 获取对应的数据库适配器（如 MySQLAdapter）
    adapter = adapter_provider(source.source_id)
    # 获取该数据源的所有表、列、外键等元数据
    metadata = adapter.get_metadata()
    available_objects = {str(table.get("name")) for table in metadata.get("tables", []) if table.get("name")}
    required_seeds = _required_schema_seeds(question, source.source_id, available_objects)
    # Deterministic selection is retained as a bounded fallback/reranker.
    lexical_selected = select_relevant_tables(
        question,
        metadata,
        max_tables=object_limit,
        data_source=source.source_id,
    )
    selected = list(dict.fromkeys(required_seeds + lexical_selected))[:object_limit]
    semantic_object_hits: list[dict] = []
    store = create_metadata_store()
    if store:
        try:
            semantic_hits = store.search(question, kind="object", source_id=source.source_id, limit=object_limit)
            semantic_selected = [str(hit.get("object_id")) for hit in semantic_hits if str(hit.get("object_id")) in available_objects]
            semantic_object_hits = [
                {
                    "object_id": str(hit.get("object_id")),
                    "vector_score": float(hit.get("vector_score", 0.0)),
                    "accepted": str(hit.get("object_id")) in available_objects,
                }
                for hit in semantic_hits
            ]
            # Required intent seeds are kept first.  They are followed by two
            # lexical seeds, semantic candidates, then remaining lexical
            # candidates.  This keeps a related-table group intact without
            # turning the whole database schema into planner input.
            selected = list(
                dict.fromkeys(required_seeds + lexical_selected[:2] + semantic_selected + lexical_selected[2:])
            )[:object_limit]
        except Exception:
            selected = lexical_selected
    # 在 selected 的基础上，自动补全关联表。
    closure = expand_schema_closure(
        metadata,
        selected,
        data_source=source.source_id,
        max_objects=object_limit,
    )
    # 从完整元数据中，只保留 closure 中的表。
    # 只保留相关表
    closure_set = set(closure)
    tables = [table for table in metadata.get("tables", []) if table.get("name") in closure_set]
    return MetadataContext(
        source=source,
        selected_object_ids=selected, # 初始筛选的表
        schema_closure_object_ids=closure, # 补全后的表
        schema_signature=str(metadata.get("schema_signature", "")),
        tables=tables,  # 表的详细元数据
        lexical_selected_object_ids=lexical_selected,
        semantic_object_hits=semantic_object_hits,
    )
