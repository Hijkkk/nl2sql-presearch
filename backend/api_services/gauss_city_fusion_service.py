"""Controlled Amap + Gauss city fusion for the NL2SQL MVP.

The LLM never generates an API call or interpolates a location into SQL.  This
service obtains a browser-authorized location from Amap, normalizes its city,
and passes that city as a bound parameter to fixed read-only Gauss queries.
"""
import time
from typing import Any, Dict, Optional

from backend.adapters.base import BaseDataSourceAdapter
from backend.api_services.amap_lbs_service import AmapLBSService
from backend.models.models import ChatResponse


class GaussCityFusionService:
    LOCATION_TERMS = ("我所在城市", "我所在的城市", "所在城市", "当前位置", "当前城市", "我现在所在")
    CONSUMPTION_TERMS = ("消费", "额度", "消费额", "消费金额", "订单金额", "销售额")
    TOTAL_TERMS = ("总消费", "总额", "总共", "合计", "多少", "总金额")

    def __init__(self, location_service: Optional[AmapLBSService] = None):
        self.location_service = location_service or AmapLBSService()

    @classmethod
    def can_handle(cls, question: str, data_source: str) -> bool:
        return data_source == "gauss_ecommerce" and any(term in question for term in cls.LOCATION_TERMS)

    def answer(
        self,
        question: str,
        client_location: Optional[Dict[str, Any]],
        adapter: BaseDataSourceAdapter,
    ) -> ChatResponse:
        started = time.perf_counter()
        try:
            location_started = time.perf_counter()
            location = self.location_service.resolve_city_from_client_location(client_location)
            location_seconds = time.perf_counter() - location_started
            city = location["city"]

            sql, mode = self._query_for(question)
            database_started = time.perf_counter()
            rows, columns = adapter.execute_query(sql, (city,))
            database_seconds = time.perf_counter() - database_started
            total_seconds = time.perf_counter() - started

            answer = self._answer_for(mode, city, rows)
            return ChatResponse(
                success=True,
                question=question,
                sql=sql,
                results=rows,
                columns=columns,
                row_count=len(rows),
                execution_time=round(total_seconds, 3),
                answer=answer,
                insight=answer,
                llm_thought=(
                    f"融合查询：浏览器定位经高德逆地理编码得到“{location['raw_city']}”，"
                    f"按高斯 customers.city 的存储规范标准化为“{city}”，再执行参数化只读查询。"
                ),
                stage_timings={
                    "location_resolution": round(location_seconds, 3),
                    "database": round(database_seconds, 3),
                    "result_summary": 0.0,
                    "total": round(total_seconds, 3),
                },
            )
        except Exception as exc:
            total_seconds = time.perf_counter() - started
            return ChatResponse(
                success=False,
                question=question,
                error=f"所在城市融合查询失败: {exc}",
                execution_time=round(total_seconds, 3),
                llm_thought="融合查询需要浏览器定位授权、高德逆地理编码和高斯数据库均可用。",
                stage_timings={"total": round(total_seconds, 3)},
            )

    def _query_for(self, question: str) -> tuple[str, str]:
        has_consumption = any(term in question for term in self.CONSUMPTION_TERMS)
        wants_total = any(term in question for term in self.TOTAL_TERMS)
        if has_consumption and wants_total:
            return (
                """
SELECT
  c.city,
  COUNT(DISTINCT c.id) AS customer_count,
  COALESCE(SUM(o.total_amount), 0) AS total_consumption
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
WHERE c.city = %s
GROUP BY c.city;
""".strip(),
                "consumption_total",
            )
        if has_consumption:
            return (
                """
SELECT
  c.id,
  c.name,
  c.city,
  c.vip_level,
  COALESCE(SUM(o.total_amount), 0) AS total_consumption
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
WHERE c.city = %s
GROUP BY c.id, c.name, c.city, c.vip_level
ORDER BY total_consumption DESC, c.id;
""".strip(),
                "customer_consumption",
            )
        if any(term in question for term in ("多少", "数量", "几名", "几个")):
            return (
                "SELECT city, COUNT(*) AS customer_count FROM customers WHERE city = %s GROUP BY city;",
                "customer_count",
            )
        return (
            """
SELECT id, name, city, register_date, vip_level
FROM customers
WHERE city = %s
ORDER BY id;
""".strip(),
            "customer_list",
        )

    @staticmethod
    def _answer_for(mode: str, city: str, rows: list[Dict[str, Any]]) -> str:
        if not rows:
            return f"高德定位解析为“{city}”，但高斯数据库中没有该城市的客户。"
        if mode == "customer_count":
            return f"高德定位解析为“{city}”，该城市共有 {rows[0].get('customer_count', 0)} 名客户。"
        if mode == "consumption_total":
            row = rows[0]
            return f"高德定位解析为“{city}”，该城市有 {row.get('customer_count', 0)} 名客户，累计消费额度为 {row.get('total_consumption', 0)}。"
        if mode == "customer_consumption":
            return f"高德定位解析为“{city}”，已返回 {len(rows)} 名客户的累计消费额度。"
        return f"高德定位解析为“{city}”，已返回 {len(rows)} 名客户。"
