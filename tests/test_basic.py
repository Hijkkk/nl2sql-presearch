"""
基础功能测试脚本
运行方式：在项目根目录执行
python -m pytest tests/test_basic.py -v
或直接 python tests/test_basic.py
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.adapters.sqlite_adapter import SQLiteAdapter
from backend.security.query_guard import QueryGuard
from backend.config.config import settings


def test_sqlite_adapter():
    print("=== 测试 SQLiteAdapter ===")
    adapter = SQLiteAdapter()
    meta = adapter.get_metadata()
    assert meta["total_tables"] == 3
    print(f"✅ 元数据获取成功，共 {meta['total_tables']} 张表")
    
    results, columns = adapter.execute_query("SELECT name FROM departments")
    assert len(results) == 3
    print(f"✅ 查询执行成功，返回 {len(results)} 行")


def test_query_guard():
    print("\n=== 测试 QueryGuard 安全校验 ===")
    
    # 正常 SELECT
    safe, err = QueryGuard.validate_read_only("SELECT * FROM employees", "sqlite")
    assert safe is True
    print("✅ 正常 SELECT 通过")
    
    # 危险 DROP
    safe, err = QueryGuard.validate_read_only("DROP TABLE employees", "sqlite")
    assert safe is False
    print(f"✅ 危险 DROP 被拦截: {err}")
    
    # 危险 INSERT
    safe, err = QueryGuard.validate_read_only("INSERT INTO employees VALUES (1)", "sqlite")
    assert safe is False
    print(f"✅ 危险 INSERT 被拦截: {err}")


def test_config():
    print("\n=== 测试配置 ===")
    print(f"Provider : {settings.llm_provider}")
    print(f"Model    : {settings.llm_model}")
    print(f"Base URL : {settings.llm_base_url}")
    print("✅ 配置加载正常")


if __name__ == "__main__":
    test_config()
    test_sqlite_adapter()
    test_query_guard()
    print("\n🎉 所有基础测试通过！")
