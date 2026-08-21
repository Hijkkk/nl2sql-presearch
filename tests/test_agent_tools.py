from backend.agent.contracts import SourceDescriptor
from backend.agent.tools import discover_sources, retrieve_metadata_context


def test_discover_sources_uses_the_single_explicit_domain_source():
    sources = [
        SourceDescriptor(source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警情、人员和地址数据"),
        SourceDescriptor(source_id="postgres_stock", source_type="postgresql", dialect="postgres", description="股票行情数据"),
    ]

    candidates = discover_sources("统计本月警情数量", allowed_sources=sources)

    assert [candidate.source_id for candidate in candidates] == ["mysql_police_address"]


def test_discover_sources_matches_chinese_business_terms_by_bigrams():
    sources = [
        SourceDescriptor(source_id="sqlite_demo", source_type="sqlite", dialect="sqlite", description="员工、部门和销售数据"),
        SourceDescriptor(source_id="postgres_stock", source_type="postgresql", dialect="postgres", description="股票行情数据"),
    ]

    candidates = discover_sources("统计各部门销售总额", allowed_sources=sources)

    assert candidates[0].source_id == "sqlite_demo"
    assert candidates[0].score > 0


def test_discover_sources_preserves_explicit_sqlite_domain_over_higher_semantic_score(monkeypatch):
    sources = [
        SourceDescriptor(source_id="sqlite_demo", source_type="sqlite", dialect="sqlite", description="员工、部门和销售数据"),
        SourceDescriptor(source_id="gauss_ecommerce", source_type="gauss", dialect="postgres", description="电商数据"),
    ]

    class FakeStore:
        def search(self, *_args, **_kwargs):
            return [
                {"source_id": "gauss_ecommerce", "vector_score": 0.99},
                {"source_id": "sqlite_demo", "vector_score": 0.01},
            ]

    monkeypatch.setattr("backend.agent.tools.create_metadata_store", lambda: FakeStore())

    candidates = discover_sources("统计每个部门分别有多少名员工", allowed_sources=sources, limit=3)

    assert [candidate.source_id for candidate in candidates] == ["sqlite_demo"]


def test_retrieve_metadata_context_returns_relationship_complete_bounded_schema():
    source = SourceDescriptor(source_id="demo", source_type="sqlite", dialect="sqlite", description="订单数据")

    class FakeAdapter:
        def get_metadata(self):
            return {
                "schema_signature": "test-signature",
                "tables": [
                    {"name": "orders", "columns": [], "foreign_keys": []},
                    {"name": "order_items", "columns": [], "foreign_keys": [{"column": "order_id", "ref_table": "orders", "ref_column": "id"}, {"column": "product_id", "ref_table": "products", "ref_column": "id"}]},
                    {"name": "products", "columns": [], "foreign_keys": []},
                ],
            }

    context = retrieve_metadata_context(
        "查询订单商品", source, object_limit=3, adapter_provider=lambda _: FakeAdapter()
    )

    assert context.schema_signature == "test-signature"
    assert "order_items" in context.schema_closure_object_ids
    assert "products" in context.schema_closure_object_ids
    assert len(context.tables) <= 3


def test_police_alert_person_intent_keeps_the_required_relationship_group():
    source = SourceDescriptor(
        source_id="mysql_police_address", source_type="mysql", dialect="mysql", description="警情、人员和地址数据"
    )

    class FakeAdapter:
        def get_metadata(self):
            return {
                "schema_signature": "police-signature",
                "tables": [
                    {"name": "police_alert", "columns": [], "foreign_keys": []},
                    {
                        "name": "alert_involvement",
                        "columns": [],
                        "foreign_keys": [{"ref_table": "police_alert"}, {"ref_table": "dict_alert_role"}],
                    },
                    {"name": "dict_alert_role", "columns": [], "foreign_keys": []},
                    {"name": "addr_alias", "columns": [], "foreign_keys": []},
                    {"name": "addr_building", "columns": [], "foreign_keys": []},
                ],
            }

    context = retrieve_metadata_context(
        "\u67e5\u8be2 2026 \u5e74 1 \u6708\u62a5\u8b66\u4e2d\u6d89\u53ca\u5acc\u7591\u4eba\u7684\u62a5\u8b66\u8bb0\u5f55",
        source,
        object_limit=5,
        adapter_provider=lambda _: FakeAdapter(),
    )

    assert {"police_alert", "alert_involvement", "dict_alert_role"}.issubset(context.selected_object_ids)
    assert {"police_alert", "alert_involvement", "dict_alert_role"}.issubset(context.schema_closure_object_ids)
    assert len(context.tables) <= 5
