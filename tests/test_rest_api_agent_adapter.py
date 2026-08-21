from backend.adapters.rest_api_adapter import RESTAPIAdapter


def test_rest_adapter_owns_controlled_amap_dispatch(monkeypatch):
    class FakeAmapService:
        def can_handle(self, question):
            return question == "天安门的经纬度是多少？"

        def answer(self, question, client_location):
            return {"question": question, "client_location": client_location}

    monkeypatch.setattr("backend.api_services.amap_lbs_service.AmapLBSService", FakeAmapService)
    adapter = RESTAPIAdapter(name="rest_api_demo", url="https://example.invalid")

    assert adapter.can_handle_controlled_question("天安门的经纬度是多少？") is True
    assert adapter.execute_controlled_question("天安门的经纬度是多少？", {"latitude": 1}) == {
        "question": "天安门的经纬度是多少？",
        "client_location": {"latitude": 1},
    }
