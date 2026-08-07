from backend.nl2sql.schema_context import expand_schema_closure


def _metadata():
    return {
        "tables": [
            {"name": "orders", "columns": [], "foreign_keys": []},
            {
                "name": "order_items",
                "columns": [],
                "foreign_keys": [
                    {"column": "order_id", "ref_table": "orders", "ref_column": "id"},
                    {"column": "product_id", "ref_table": "products", "ref_column": "id"},
                ],
            },
            {"name": "categories", "columns": [], "foreign_keys": []},
            {"name": "products", "columns": [], "foreign_keys": []},
        ]
    }


def test_schema_closure_appends_join_target_missing_from_initial_selection():
    objects = expand_schema_closure(
        _metadata(),
        ["orders", "order_items", "categories"],
        max_objects=5,
    )

    assert objects == ["orders", "order_items", "categories", "products"]


def test_schema_closure_follows_reverse_foreign_key_relationships():
    objects = expand_schema_closure(_metadata(), ["products"], max_objects=5)

    assert objects[:2] == ["products", "order_items"]


def test_schema_closure_respects_the_prompt_object_budget():
    objects = expand_schema_closure(_metadata(), ["orders", "order_items"], max_objects=3)

    assert objects == ["orders", "order_items", "products"]

if __name__ == "__main__":
    test_schema_closure_appends_join_target_missing_from_initial_selection()