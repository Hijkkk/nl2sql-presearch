import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.adapters.rest_api_adapter import RESTAPIAdapter


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}, "timeout": timeout})
        return FakeResponse(self.payload)


class ParamFakeClient(FakeClient):
    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}, "timeout": timeout})
        return FakeResponse(self.payload)


class TimeoutClient:
    def get(self, url, headers=None, params=None, timeout=None):
        request = httpx.Request("GET", url)
        raise httpx.ConnectTimeout("_ssl.c:999: The handshake operation timed out", request=request)


def test_rest_api_adapter_metadata_and_query():
    client = FakeClient(
        {
            "code": 0,
            "data": [
                {"id": 1, "name": "华东客户", "amount": 1200.5, "active": True},
                {"id": 2, "name": "华北客户", "amount": 800.0, "active": False},
            ],
        }
    )
    adapter = RESTAPIAdapter(
        name="rest_api_demo",
        url="https://example.test/customers",
        table_name="customers",
        data_path="data",
        headers={"Authorization": "Bearer test"},
        http_client=client,
    )

    metadata = adapter.get_metadata()
    assert metadata["total_tables"] == 1
    assert metadata["tables"][0]["name"] == "customers"
    assert {column["name"] for column in metadata["tables"][0]["columns"]} == {
        "id",
        "name",
        "amount",
        "active",
    }

    results, columns = adapter.execute_query(
        "SELECT name, amount FROM customers WHERE amount > 1000 ORDER BY amount DESC"
    )

    assert columns == ["name", "amount"]
    assert results == [{"name": "华东客户", "amount": 1200.5}]
    assert client.calls[0]["headers"] == {"Authorization": "Bearer test"}


def test_rest_api_adapter_flattens_nested_objects():
    client = FakeClient(
        [
            {
                "id": 1,
                "profile": {"city": "上海", "level": "A"},
                "tags": ["vip", "finance"],
            }
        ]
    )
    adapter = RESTAPIAdapter(
        name="rest_api_demo",
        url="https://example.test/users",
        table_name="api_users",
        http_client=client,
    )

    results, columns = adapter.execute_query(
        "SELECT profile_city, profile_level, tags FROM api_users WHERE id = 1"
    )

    assert columns == ["profile_city", "profile_level", "tags"]
    assert results[0]["profile_city"] == "上海"
    assert results[0]["profile_level"] == "A"
    assert "vip" in results[0]["tags"]


def test_rest_api_adapter_reuses_short_cache():
    client = FakeClient([{"id": 1, "name": "cached"}])
    adapter = RESTAPIAdapter(
        name="rest_api_demo",
        url="https://example.test/cache",
        table_name="items",
        http_client=client,
    )

    adapter.get_metadata()
    adapter.execute_query("SELECT id, name FROM items")

    assert len(client.calls) == 1


def test_rest_api_adapter_connect_timeout_has_readable_error():
    adapter = RESTAPIAdapter(
        name="rest_api_demo",
        url="https://example.test/timeout",
        table_name="items",
        http_client=TimeoutClient(),
        timeout=3,
    )

    with pytest.raises(RuntimeError, match="REST API 连接超时"):
        adapter.get_metadata()


def test_rest_api_adapter_supports_amap_style_query_params_and_data_path():
    client = ParamFakeClient(
        {
            "status": "1",
            "count": "1",
            "info": "OK",
            "lives": [
                {
                    "province": "北京",
                    "city": "东城区",
                    "adcode": "110101",
                    "weather": "晴",
                    "temperature": "31",
                    "humidity": "40",
                }
            ],
        }
    )
    adapter = RESTAPIAdapter(
        name="rest_api_demo",
        url="https://restapi.amap.com/v3/weather/weatherInfo",
        table_name="amap_weather",
        data_path="lives",
        query_params={"city": "110101", "extensions": "base", "output": "JSON"},
        api_key_param="key",
        api_key="test-key",
        http_client=client,
    )

    results, columns = adapter.execute_query("SELECT city, weather, temperature FROM amap_weather")

    assert columns == ["city", "weather", "temperature"]
    assert results == [{"city": "东城区", "weather": "晴", "temperature": "31"}]
    assert client.calls[0]["params"] == {
        "city": "110101",
        "extensions": "base",
        "output": "JSON",
        "key": "test-key",
    }


def test_rest_api_adapter_reports_business_status_error():
    client = ParamFakeClient({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
    adapter = RESTAPIAdapter(
        name="rest_api_demo",
        url="https://restapi.amap.com/v3/weather/weatherInfo",
        table_name="amap_weather",
        data_path="lives",
        http_client=client,
    )

    with pytest.raises(RuntimeError, match="INVALID_USER_KEY"):
        adapter.get_metadata()
