import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.api_services.amap_lbs_service import AmapLBSService


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAmapClient:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "timeout": timeout})
        if "weather/weatherInfo" in url:
            if params.get("city") != "110101":
                return FakeResponse({"status": "1", "lives": []})
            return FakeResponse({
                "status": "1",
                "lives": [{"city": "东城区", "weather": "晴", "temperature": "31", "humidity": "40"}],
            })
        if "geocode/regeo" in url:
            return FakeResponse({
                "status": "1",
                "regeocode": {
                    "formatted_address": "北京市东城区天安门",
                    "addressComponent": {"adcode": "110101"},
                },
            })
        if "geocode/geo" in url:
            address = params.get("address", "")
            location = "116.397428,39.90923" if "天安门" in address else "116.481488,39.990464"
            return FakeResponse({"status": "1", "geocodes": [{"formatted_address": address, "location": location}]})
        if "distance" in url:
            return FakeResponse({"status": "1", "results": [{"distance": "12000", "duration": "1800"}]})
        if "place/around" in url:
            return FakeResponse({
                "status": "1",
                "pois": [{"name": "示例餐馆", "type": "餐饮服务", "distance": "350"}],
            })
        if "config/district" in url:
            keyword = params.get("keywords", "")
            adcode = "110101" if "东城" in keyword else "110000"
            name = "东城区" if adcode == "110101" else "北京市"
            return FakeResponse({"status": "1", "districts": [{"name": name, "adcode": adcode, "level": "district"}]})
        if url.endswith("/v3/ip"):
            return FakeResponse({
                "status": "1",
                "province": "北京市",
                "city": "北京市",
                "adcode": "110000",
                "rectangle": "116.0119343,39.66127144;116.7829835,40.2164962",
            })
        return FakeResponse({"status": "1"})


def test_amap_weather_question():
    client = FakeAmapClient()
    service = AmapLBSService(http_client=client)

    response = service.answer("北京东城区天气怎么样")

    assert response.success is True
    assert response.results[0]["weather"] == "晴"
    assert response.results[0]["query_city_code"] == "110101"
    assert response.sql == "GET /v3/weather/weatherInfo"
    assert any("config/district" in call["url"] for call in client.calls)


def test_amap_distance_question_resolves_addresses():
    client = FakeAmapClient()
    service = AmapLBSService(http_client=client)

    response = service.answer("从天安门到望京SOHO的距离是多远")

    assert response.success is True
    assert response.results[0]["distance"] == "12000"
    assert response.results[0]["distance_text"] == "12.00 公里"
    assert response.results[0]["duration_text"] == "30分钟0秒"
    assert response.sql == "GET /v3/distance"
    assert any("geocode/geo" in call["url"] for call in client.calls)


def test_amap_district_question():
    service = AmapLBSService(http_client=FakeAmapClient())

    response = service.answer("查询北京市行政区域")

    assert response.success is True
    assert response.results[0]["adcode"] == "110000"


def test_amap_current_city_question_uses_ip_location():
    service = AmapLBSService(http_client=FakeAmapClient())

    response = service.answer("我现在大概在哪个城市")

    assert response.success is True
    assert response.sql == "GET /v3/ip"
    assert response.results[0]["city"] == "北京市"
    assert response.results[0]["location_source"] == "ip"


def test_amap_distance_from_browser_location():
    client = FakeAmapClient()
    service = AmapLBSService(http_client=client)

    response = service.answer(
        "我当前位置到北京南站多远",
        client_location={"latitude": 39.90923, "longitude": 116.397428, "accuracy": 30},
    )

    assert response.success is True
    assert response.sql == "GET /v3/distance"
    assert response.results[0]["origin_location_source"] == "browser_geolocation"
    assert response.results[0]["destination_text"] == "北京南站"
    distance_call = next(call for call in client.calls if "distance" in call["url"])
    assert distance_call["params"]["origins"] == "116.397428,39.909230"


def test_amap_around_search_from_browser_location():
    service = AmapLBSService(http_client=FakeAmapClient())

    response = service.answer(
        "我当前位置附近有什么餐馆",
        client_location={"lat": 39.90923, "lng": 116.397428},
    )

    assert response.success is True
    assert response.sql == "GET /v3/place/around"
    assert response.results[0]["name"] == "示例餐馆"
    assert response.results[0]["location_source"] == "browser_geolocation"


def test_amap_weather_from_browser_location_uses_regeo_adcode():
    client = FakeAmapClient()
    service = AmapLBSService(http_client=client)

    response = service.answer(
        "我现在位置天气怎么样",
        client_location={"latitude": 39.90923, "longitude": 116.397428},
    )

    assert response.success is True
    assert response.sql == "GET /v3/weather/weatherInfo"
    assert response.results[0]["query_city_code"] == "110101"
    assert response.results[0]["location_source"] == "browser_geolocation"
    assert any("geocode/regeo" in call["url"] for call in client.calls)
