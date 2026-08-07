from backend.api_services.amap_lbs_service import AmapLBSService
from backend.api_services.gauss_city_fusion_service import GaussCityFusionService


class FakeLocationService:
    def resolve_city_from_client_location(self, client_location):
        assert client_location == {"latitude": 30.57, "longitude": 104.06}
        return {"city": "成都", "raw_city": "成都市", "adcode": "510100", "formatted_address": "成都市"}


class FakeGaussAdapter:
    def __init__(self, rows, columns):
        self.rows = rows
        self.columns = columns
        self.calls = []

    def execute_query(self, sql, params=None):
        self.calls.append((sql, params))
        return self.rows, self.columns


def test_amap_city_normalization_matches_gauss_customers_city_format():
    assert AmapLBSService.normalize_business_city("成都市") == "成都"
    assert AmapLBSService.normalize_business_city("上海") == "上海"
    assert AmapLBSService.normalize_business_city(" 成 都 市 ") == "成都"


def test_gauss_city_fusion_lists_customers_with_bound_city_parameter():
    adapter = FakeGaussAdapter([{"id": 1, "name": "张伟", "city": "成都"}], ["id", "name", "city"])
    service = GaussCityFusionService(FakeLocationService())

    response = service.answer(
        "我所在城市有哪些客户？",
        {"latitude": 30.57, "longitude": 104.06},
        adapter,
    )

    assert response.success is True
    assert response.results == [{"id": 1, "name": "张伟", "city": "成都"}]
    assert "WHERE city = %s" in response.sql
    assert "email" not in response.sql.lower()
    assert "phone" not in response.sql.lower()
    assert adapter.calls[0][1] == ("成都",)
    assert "成都市" in response.llm_thought
    assert "成都" in response.answer


def test_gauss_city_fusion_uses_customer_consumption_query():
    adapter = FakeGaussAdapter(
        [{"id": 1, "name": "张伟", "city": "成都", "total_consumption": 88.5}],
        ["id", "name", "city", "total_consumption"],
    )
    service = GaussCityFusionService(FakeLocationService())

    response = service.answer(
        "我所在城市用户的消费额度是多少？",
        {"latitude": 30.57, "longitude": 104.06},
        adapter,
    )

    assert response.success is True
    assert "LEFT JOIN orders" in response.sql
    assert "SUM(o.total_amount)" in response.sql
    assert adapter.calls[0][1] == ("成都",)


def test_gauss_city_fusion_only_handles_gauss_location_questions():
    assert GaussCityFusionService.can_handle("我所在城市有哪些客户？", "gauss_ecommerce")
    assert not GaussCityFusionService.can_handle("成都有哪些客户？", "gauss_ecommerce")
    assert not GaussCityFusionService.can_handle("我所在城市有哪些客户？", "dameng_ecommerce")
