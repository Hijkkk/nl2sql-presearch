import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.adapters.hive_adapter import HadoopLocalDemoAdapter


def make_adapter():
    data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "hadoop"))
    return HadoopLocalDemoAdapter(
        name="hive_hadoop_demo",
        data_dir=data_dir,
    )


def test_hadoop_local_demo_adapter_exposes_hdfs_like_csv_metadata():
    adapter = make_adapter()

    metadata = adapter.get_metadata()

    assert metadata["total_tables"] == 4
    tables = {table["name"]: table for table in metadata["tables"]}
    assert {
        "hadoop_order_events",
        "hadoop_user_profiles",
        "hadoop_product_dim",
        "hadoop_region_dim",
    } <= set(tables)

    order_columns = {column["name"] for column in tables["hadoop_order_events"]["columns"]}
    assert {"event_id", "event_date", "user_id", "product_id", "region_id", "order_count", "gmv"} <= order_columns
    assert tables["hadoop_order_events"]["primary_key"] == ["event_id"]
    assert {fk["ref_table"] for fk in tables["hadoop_order_events"]["foreign_keys"]} == {
        "hadoop_user_profiles",
        "hadoop_product_dim",
        "hadoop_region_dim",
    }


def test_hadoop_local_demo_adapter_executes_sql_over_multi_table_csv_data():
    adapter = make_adapter()

    rows, columns = adapter.execute_query(
        """
        SELECT r.city AS 城市, ROUND(SUM(e.gmv), 2) AS 成交金额
        FROM hadoop_order_events e
        JOIN hadoop_region_dim r ON e.region_id = r.region_id
        GROUP BY r.city
        ORDER BY 成交金额 DESC
        LIMIT 3
        """
    )

    assert columns == ["城市", "成交金额"]
    assert len(rows) == 3
    assert rows[0]["成交金额"] >= rows[1]["成交金额"]


def test_hadoop_local_demo_adapter_supports_user_product_region_join():
    adapter = make_adapter()

    rows, columns = adapter.execute_query(
        """
        SELECT u.user_name AS 用户, p.brand AS 品牌, r.city AS 城市, ROUND(SUM(e.gmv), 2) AS 成交金额
        FROM hadoop_order_events e
        JOIN hadoop_user_profiles u ON e.user_id = u.user_id
        JOIN hadoop_product_dim p ON e.product_id = p.product_id
        JOIN hadoop_region_dim r ON e.region_id = r.region_id
        WHERE u.vip_level = 5
        GROUP BY u.user_name, p.brand, r.city
        ORDER BY 成交金额 DESC
        LIMIT 5
        """
    )

    assert columns == ["用户", "品牌", "城市", "成交金额"]
    assert rows
    assert all(row["成交金额"] > 0 for row in rows)
