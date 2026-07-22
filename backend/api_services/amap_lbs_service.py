"""
高德 LBS API 服务编排器

职责：
- 复用通用 REST API 配置中的 key、base url 和超时。
- 根据自然语言问题选择高德服务。
- 从问题中抽取简单参数，调用对应 endpoint。
- 返回 ChatResponse 可直接展示的表格结果。
# 只是调 API + 解析数据

这不是替代 RESTAPIAdapter，而是建立在 REST API 基础设施之上的服务级适配层。
"""
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from backend.config.config import settings
from backend.models.models import ChatResponse


class AmapLBSService:
    def __init__(self, http_client: Optional[httpx.Client] = None):
        self.base_url = settings.rest_api_url
        if "/v3/" in self.base_url:
            self.base_url = self.base_url.split("/v3/", 1)[0]
        self.api_key = settings.rest_api_api_key
        self.key_param = settings.rest_api_key_param or "key"
        self.timeout = settings.rest_api_timeout
        self.client = http_client or httpx.Client(timeout=self.timeout)

    def can_handle(self, question: str) -> bool:
        if not self.api_key:
            return False
        keywords = [
            "天气", "温度", "湿度", "下雨", "晴",
            "距离", "多远", "路径", "路线", "驾车", "步行", "骑行",
            "行政区", "行政区域", "区划", "adcode",
            "经纬度", "地理编码", "地址", "逆地理",
            "附近", "周边", "搜索", "查询", "定位", "ip",
            "现在", "当前位置", "当前城市", "哪个城市", "在哪", "大概在哪", "所在城市",
        ]
        # 用户问："北京天气怎么样" → 包含"天气" → True
        # 用户问："查询销售额" → 不包含任何关键词 → False
        # any() 是 Python 内置函数，只要 iterable 中有一个元素为 True，就返回 True，否则返回 False。
        return any(keyword in question.lower() for keyword in keywords)

    # 处理用户问题，返回 ChatResponse。
    def answer(self, question: str, client_location: Optional[Dict[str, Any]] = None) -> ChatResponse:
        start = time.time()
        try:
            service = self._select_service(question, client_location=client_location)  # 1. 选择要调的高德服务
            if service.get("requires_client_location"):
                rows = service["handler"](question, client_location)  # 2. 调用对应的处理函数
            else:
                rows = service["handler"](question)
            columns = self._collect_columns(rows)   # 3. 收集字段名
            return ChatResponse(
                success=True,
                question=question,
                sql=f"GET {service['endpoint']}",   # 显示调了哪个 API
                results=rows,  # 返回数据
                columns=columns,
                row_count=len(rows),
                execution_time=round(time.time() - start, 2),
                llm_thought=f"已识别为高德地图 {service['name']} 服务调用。",
                insight=self._build_insight(service["name"], rows),   # 生成自然语言总结
            )
        except Exception as exc:
            return ChatResponse(
                success=False,
                question=question,
                error=f"高德 API 调用失败: {exc}",
                execution_time=round(time.time() - start, 2),
                llm_thought="问题已进入高德 LBS 服务编排器，但参数抽取或接口调用失败。",
            )

    def _select_service(
        self,
        question: str,
        client_location: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        client_location_service = self._select_client_location_service(question, client_location)
        if client_location_service:
            return client_location_service

        if self._is_current_location_question(question):
            return {"name": "IP定位", "endpoint": "/v3/ip", "handler": self._ip_location}
        if any(word in question for word in ["天气", "温度", "湿度", "下雨", "晴"]):
            return {"name": "天气查询", "endpoint": "/v3/weather/weatherInfo", "handler": self._weather}
        if any(word in question for word in ["距离", "多远"]):
            return {"name": "距离测量", "endpoint": "/v3/distance", "handler": self._distance}
        if any(word in question for word in ["行政区", "行政区域", "区划", "adcode"]):
            return {"name": "行政区域查询", "endpoint": "/v3/config/district", "handler": self._district}
        if any(word in question for word in ["经纬度", "地理编码"]):
            return {"name": "地理编码", "endpoint": "/v3/geocode/geo", "handler": self._geocode}
        if "逆地理" in question:
            return {"name": "逆地理编码", "endpoint": "/v3/geocode/regeo", "handler": self._regeo}
        if any(word in question for word in ["驾车", "路线", "路径"]):
            return {"name": "驾车路径规划", "endpoint": "/v3/direction/driving", "handler": self._driving}
        if any(word in question for word in ["步行"]):
            return {"name": "步行路径规划", "endpoint": "/v3/direction/walking", "handler": self._walking}
        if any(word in question for word in ["骑行"]):
            return {"name": "骑行路径规划", "endpoint": "/v4/direction/bicycling", "handler": self._bicycling}
        if any(word in question for word in ["附近", "周边"]):
            return {"name": "周边搜索", "endpoint": "/v3/place/around", "handler": self._around_search}
        if any(word in question for word in ["搜索", "查询"]):
            return {"name": "关键字搜索", "endpoint": "/v3/place/text", "handler": self._text_search}
        raise ValueError("暂未识别出可调用的高德服务，请换成天气、距离、行政区、地理编码、路线、周边搜索等问题")

    def _select_client_location_service(
        self,
        question: str,
        client_location: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not client_location or not self._mentions_current_location(question):
            return None

        common = {"requires_client_location": True}
        if any(word in question for word in ["距离", "多远", "到"]):
            return {
                **common,
                "name": "当前位置距离测量",
                "endpoint": "/v3/distance",
                "handler": self._distance_from_client_location,
            }
        if any(word in question for word in ["附近", "周边"]):
            return {
                **common,
                "name": "当前位置周边搜索",
                "endpoint": "/v3/place/around",
                "handler": self._around_search_from_client_location,
            }
        if any(word in question for word in ["天气", "温度", "湿度", "下雨", "晴"]):
            return {
                **common,
                "name": "当前位置天气查询",
                "endpoint": "/v3/weather/weatherInfo",
                "handler": self._weather_from_client_location,
            }
        return None

    def _weather(self, question: str) -> List[Dict[str, Any]]:
        query_city = self._extract_city(question)
        attempted: List[str] = []

        for city in self._weather_city_candidates(query_city):
            attempted.append(city)
            payload = self._get("/v3/weather/weatherInfo", {"city": city, "extensions": "base", "output": "JSON"})
            rows = self._ensure_rows(payload.get("lives", []))
            if rows:
                for row in rows:
                    row["query_city"] = query_city or city
                    row["query_city_code"] = city
                return rows

        raise ValueError(f"未查询到天气结果，已尝试城市/adcode：{', '.join(attempted)}")

    def _distance(self, question: str) -> List[Dict[str, Any]]:
        origin, destination = self._extract_origin_destination(question)
        origin_location = self._resolve_location(origin)
        destination_location = self._resolve_location(destination)
        payload = self._get("/v3/distance", {
            "origins": origin_location,
            "destination": destination_location,
            "type": "1",
            "output": "JSON",
        })
        rows = self._ensure_rows(payload.get("results", []))
        for row in rows:
            row["origin_text"] = origin
            row["destination_text"] = destination
        return self._decorate_distance_rows(rows)

    def _district(self, question: str) -> List[Dict[str, Any]]:
        keywords = self._extract_place(question) or self._default_city()
        payload = self._get("/v3/config/district", {
            "keywords": keywords,
            "subdistrict": "1",
            "extensions": "base",
            "output": "JSON",
        })
        return self._flatten_districts(payload.get("districts", []))

    def _geocode(self, question: str) -> List[Dict[str, Any]]:
        address = self._extract_place(question)
        if not address:
            raise ValueError("请在问题中提供要地理编码的地址")
        payload = self._get("/v3/geocode/geo", {"address": address, "output": "JSON"})
        return self._ensure_rows(payload.get("geocodes", []))

    def _regeo(self, question: str) -> List[Dict[str, Any]]:
        location = self._extract_location(question)
        if not location:
            raise ValueError("请提供经纬度，例如 116.397428,39.90923")
        payload = self._get("/v3/geocode/regeo", {"location": location, "output": "JSON"})
        return [payload.get("regeocode", {})]

    def _driving(self, question: str) -> List[Dict[str, Any]]:
        return self._route(question, "/v3/direction/driving")

    def _walking(self, question: str) -> List[Dict[str, Any]]:
        return self._route(question, "/v3/direction/walking")

    def _bicycling(self, question: str) -> List[Dict[str, Any]]:
        return self._route(question, "/v4/direction/bicycling")

    def _route(self, question: str, endpoint: str) -> List[Dict[str, Any]]:
        origin, destination = self._extract_origin_destination(question)
        payload = self._get(endpoint, {
            "origin": self._resolve_location(origin),
            "destination": self._resolve_location(destination),
            "output": "JSON",
        })
        route = payload.get("route", {})
        paths = self._ensure_rows(route.get("paths", []))
        for row in paths:
            row["origin_text"] = origin
            row["destination_text"] = destination
        return self._decorate_distance_rows(paths)

    def _text_search(self, question: str) -> List[Dict[str, Any]]:
        keywords = self._extract_keyword(question)
        city = self._extract_city(question) or ""
        payload = self._get("/v3/place/text", {
            "keywords": keywords,
            "city": city,
            "offset": "20",
            "page": "1",
            "extensions": "base",
            "output": "JSON",
        })
        return self._ensure_rows(payload.get("pois", []))

    def _around_search(self, question: str) -> List[Dict[str, Any]]:
        keywords = self._extract_keyword(question)
        place = self._extract_place(question) or self._default_city()
        payload = self._get("/v3/place/around", {
            "keywords": keywords,
            "location": self._resolve_location(place),
            "radius": "3000",
            "offset": "20",
            "page": "1",
            "extensions": "base",
            "output": "JSON",
        })
        return self._ensure_rows(payload.get("pois", []))

    def _distance_from_client_location(
        self,
        question: str,
        client_location: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        destination = self._extract_destination_from_current_location(question)
        destination_location = self._resolve_location(destination)
        payload = self._get("/v3/distance", {
            "origins": self._format_client_location(client_location),
            "destination": destination_location,
            "type": "1",
            "output": "JSON",
        })
        rows = self._ensure_rows(payload.get("results", []))
        for row in rows:
            row["origin_text"] = "当前位置"
            row["origin_location_source"] = "browser_geolocation"
            row["destination_text"] = destination
        return self._decorate_distance_rows(rows)

    def _around_search_from_client_location(
        self,
        question: str,
        client_location: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        payload = self._get("/v3/place/around", {
            "keywords": self._extract_keyword(question),
            "location": self._format_client_location(client_location),
            "radius": "3000",
            "offset": "20",
            "page": "1",
            "extensions": "base",
            "output": "JSON",
        })
        rows = self._ensure_rows(payload.get("pois", []))
        for row in rows:
            row["location_source"] = "browser_geolocation"
        return rows

    def _weather_from_client_location(
        self,
        question: str,
        client_location: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        location = self._format_client_location(client_location)
        regeo_payload = self._get("/v3/geocode/regeo", {"location": location, "output": "JSON"})
        regeocode = regeo_payload.get("regeocode") or {}
        address_component = regeocode.get("addressComponent") or {}
        adcode = address_component.get("adcode")
        if not adcode:
            raise ValueError("无法根据浏览器定位解析行政区 adcode，不能查询当前位置天气")

        weather_payload = self._get("/v3/weather/weatherInfo", {
            "city": adcode,
            "extensions": "base",
            "output": "JSON",
        })
        rows = self._ensure_rows(weather_payload.get("lives", []))
        for row in rows:
            row["query_city_code"] = adcode
            row["location_source"] = "browser_geolocation"
            row["formatted_address"] = regeocode.get("formatted_address")
        return rows

    def _ip_location(self, question: str) -> List[Dict[str, Any]]:
        payload = self._get("/v3/ip", {"output": "JSON"})
        payload["location_source"] = "ip"
        return [payload]

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{endpoint}"
        request_params = dict(params)
        request_params[self.key_param] = self.api_key
        response = self.client.get(url, params=request_params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") in (0, "0"):
            info = payload.get("info") or payload.get("message") or "未知错误"
            infocode = payload.get("infocode")
            raise RuntimeError(f"{info}" + (f"({infocode})" if infocode else ""))
        return payload

    def _resolve_location(self, text: str) -> str:
        if self._looks_like_location(text):
            return text
        rows = self._geocode(f"{text} 经纬度")
        if not rows or not rows[0].get("location"):
            raise ValueError(f"无法解析地址坐标：{text}")
        return rows[0]["location"]

    def _extract_origin_destination(self, question: str) -> tuple[str, str]:
        patterns = [
            r"从(.+?)到(.+?)(?:的|有|距离|多远|路线|路径|怎么走|$)",
            r"(.+?)到(.+?)(?:的|有|距离|多远|路线|路径|怎么走|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, question)
            if match:
                origin = self._clean_place(match.group(1))
                destination = self._clean_place(match.group(2))
                if origin and destination:
                    return origin, destination
        raise ValueError("请用“从A到B”描述起点和终点")

    def _extract_city(self, question: str) -> str:
        match = re.search(r"([\u4e00-\u9fa5]{2,12})(?:市|区|县|州|盟)?(?:的)?(?:天气|温度|湿度)", question)
        if match:
            return self._clean_place(match.group(1))
        return ""

    def _is_current_location_question(self, question: str) -> bool:
        lower_question = question.lower()
        if "ip" in lower_question or "定位" in question:
            return True
        return self._mentions_current_location(question)

    def _mentions_current_location(self, question: str) -> bool:
        return any(word in question for word in ["我现在", "当前位置", "当前城市", "哪个城市", "在哪", "大概在哪", "所在城市", "现在位置"])

    def _weather_city_candidates(self, city_text: str) -> List[str]:
        candidates: List[str] = []
        if city_text:
            candidates.extend([city_text, *self._city_aliases(city_text)])
            adcode = self._resolve_adcode(city_text)
            if adcode:
                candidates.insert(0, adcode)

        candidates.append(self._default_city())
        return self._unique_values(candidates)

    def _resolve_adcode(self, place: str) -> str:
        for keyword in self._unique_values([place, *self._city_aliases(place)]):
            payload = self._get("/v3/config/district", {
                "keywords": keyword,
                "subdistrict": "0",
                "extensions": "base",
                "output": "JSON",
            })
            districts = self._ensure_rows(payload.get("districts", []))
            if districts and districts[0].get("adcode"):
                return str(districts[0]["adcode"])
        return ""

    def _city_aliases(self, value: str) -> List[str]:
        aliases = []
        cleaned = self._clean_place(value)
        if cleaned:
            aliases.append(cleaned)
        municipality_prefixes = ["北京", "上海", "天津", "重庆"]
        for prefix in municipality_prefixes:
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                aliases.append(cleaned[len(prefix):])
                aliases.append(f"{prefix}市")
        if cleaned.endswith(("市", "区", "县")):
            aliases.append(cleaned[:-1])
        return self._unique_values(aliases)

    def _extract_place(self, question: str) -> str:
        match = re.search(r"(?:查询|搜索|看看|获取)?(.+?)(?:的)?(?:行政区|行政区域|区划|经纬度|地理编码|天气|附近|周边)", question)
        if match:
            return self._clean_place(match.group(1))
        match = re.search(r"地址[是为：: ]+(.+)", question)
        if match:
            return self._clean_place(match.group(1))
        return ""

    def _extract_keyword(self, question: str) -> str:
        for marker in ["搜索", "查询", "找", "附近", "周边"]:
            if marker in question:
                tail = question.split(marker, 1)[1]
                tail = re.split(r"的|在|附近|周边", tail)[0]
                cleaned = self._clean_place(tail)
                cleaned = self._clean_keyword(cleaned)
                if cleaned:
                    return cleaned
        return "餐饮"

    def _extract_destination_from_current_location(self, question: str) -> str:
        patterns = [
            r"(?:我现在|当前位置|现在位置|当前我|我所在位置).{0,4}到(.+?)(?:多远|距离|怎么走|路线|路程|$)",
            r"到(.+?)(?:多远|距离|怎么走|路线|路程|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, question)
            if match:
                destination = self._clean_place(match.group(1))
                if destination:
                    return destination
        raise ValueError("请用“我当前位置到某地多远”描述目的地")

    def _format_client_location(self, client_location: Dict[str, Any]) -> str:
        longitude = client_location.get("longitude", client_location.get("lng"))
        latitude = client_location.get("latitude", client_location.get("lat"))
        try:
            longitude_float = float(longitude)
            latitude_float = float(latitude)
        except (TypeError, ValueError) as exc:
            raise ValueError("浏览器定位缺少合法的 latitude/longitude") from exc

        if not (-180 <= longitude_float <= 180 and -90 <= latitude_float <= 90):
            raise ValueError("浏览器定位经纬度超出合法范围")
        return f"{longitude_float:.6f},{latitude_float:.6f}"

    def _clean_keyword(self, value: str) -> str:
        cleaned = value
        for token in ["有什么", "有哪些", "有没有", "一下", "帮我", "附近", "周边", "的"]:
            cleaned = cleaned.replace(token, "")
        return cleaned or "餐饮"

    def _extract_location(self, question: str) -> str:
        match = re.search(r"(\d{2,3}\.\d+)\s*[,，]\s*(\d{1,2}\.\d+)", question)
        if match:
            return f"{match.group(1)},{match.group(2)}"
        return ""

    def _looks_like_location(self, text: str) -> bool:
        return bool(re.fullmatch(r"\d{2,3}\.\d+\s*,\s*\d{1,2}\.\d+", text.strip()))

    def _default_city(self) -> str:
        match = re.search(r'"city"\s*:\s*"([^"]+)"', settings.rest_api_query_params_json or "")
        return match.group(1) if match else "110101"

    def _clean_place(self, value: str) -> str:
        return re.sub(r"[，,。？?！!\s]", "", value).strip("的从到")

    def _decorate_distance_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for row in rows:
            distance_value = self._parse_int(row.get("distance"))
            if distance_value is not None:
                row["distance_m"] = distance_value
                row["distance_text"] = self._format_distance(distance_value)

            duration_value = self._parse_int(row.get("duration"))
            if duration_value is not None:
                row["duration_seconds"] = duration_value
                row["duration_text"] = self._format_duration(duration_value)
        return rows

    def _parse_int(self, value: Any) -> Optional[int]:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None

    def _format_distance(self, meters: int) -> str:
        if meters >= 1000:
            return f"{meters / 1000:.2f} 公里"
        return f"{meters} 米"

    def _format_duration(self, seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, remain_seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}小时{minutes}分钟"
        if minutes:
            return f"{minutes}分钟{remain_seconds}秒"
        return f"{remain_seconds}秒"

    def _ensure_rows(self, value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, list):
            return [item if isinstance(item, dict) else {"value": item} for item in value]
        if isinstance(value, dict):
            return [value]
        return [{"value": value}]

    def _flatten_districts(self, districts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for district in districts:
            row = {key: value for key, value in district.items() if key != "districts"}
            rows.append(row)
            children = district.get("districts") or []
            if isinstance(children, list):
                rows.extend(self._flatten_districts(children))
        return rows

    def _collect_columns(self, rows: List[Dict[str, Any]]) -> List[str]:
        columns: List[str] = []
        for row in rows:
            for key in row.keys():
                if key not in columns:
                    columns.append(key)
        return columns

    def _build_insight(self, service_name: str, rows: List[Dict[str, Any]]) -> str:
        if service_name in {"天气查询", "当前位置天气查询"} and rows:
            row = rows[0]
            city = row.get("city") or row.get("query_city") or "目标城市"
            weather = row.get("weather")
            temperature = row.get("temperature")
            if weather and temperature:
                return f"{city}当前天气为{weather}，温度 {temperature}℃。"
        if service_name in {"距离测量", "当前位置距离测量", "驾车路径规划", "步行路径规划", "骑行路径规划"} and rows:
            row = rows[0]
            distance_text = row.get("distance_text")
            duration_text = row.get("duration_text")
            if distance_text and duration_text:
                return f"两地距离约 {distance_text}，预计耗时 {duration_text}。"
            if distance_text:
                return f"两地距离约 {distance_text}。"
        if service_name == "IP定位" and rows:
            row = rows[0]
            province = row.get("province") or ""
            city = row.get("city") or ""
            if province or city:
                return f"根据高德 IP 定位，当前位置大概在{province}{city}。"
        return f"已调用高德地图{service_name}服务，返回 {len(rows)} 条记录。"

    def _unique_values(self, values: List[str]) -> List[str]:
        result: List[str] = []
        for value in values:
            if value and value not in result:
                result.append(value)
        return result
