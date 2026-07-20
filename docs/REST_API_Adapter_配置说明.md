# REST API Adapter 配置说明

## 1. 它在本项目中的定位

REST API Adapter 是后端的数据源适配器，和 SQLiteAdapter、MySQLAdapter 平级。

它不是让前端直接访问数据库，也不是替代 FastAPI。前端仍然只调用本项目后端接口：

```text
前端 Vue -> FastAPI -> RESTAPIAdapter -> 外部 REST API
```

RESTAPIAdapter 做的事情是：

1. 后端请求一个外部 JSON REST API。
2. 从响应中取出对象数组。
3. 把 JSON 字段映射成一张只读临时表。
4. 复用现有 NL2SQL 链路生成并执行 SELECT 查询。

这样可以证明项目具备“多数据源适配能力”，但不会把数据库账号、API Token、内部接口地址暴露给浏览器。

## 2. 当前已经实现的数据源

当前项目已经实现：

| 数据源类型 | 适配器 | 是否已接入 /api/v1/data-sources | 说明 |
| --- | --- | --- | --- |
| SQLite | SQLiteAdapter | 是 | 内置演示库，用于快速演示 NL2SQL |
| MySQL | MySQLAdapter | 是 | 本地业务库，和用户系统库分离 |
| REST API | RESTAPIAdapter | 是 | 外部 JSON API 映射成只读临时表 |

尚未实现：

| 数据源类型 | 当前状态 |
| --- | --- |
| MongoDB | 未实现，需要新增 MongoAdapter |
| Redis | 未实现，需要新增 RedisAdapter |
| Elasticsearch | 未实现，需要新增 ElasticsearchAdapter |
| 消息队列 | 未实现，通常不适合直接做 SQL 查询，需要先定义消息快照或消费模型 |

## 3. .env 配置项

```env
REST_API_ENABLED=true
REST_API_NAME=rest_api_demo
REST_API_URL=https://api.jsonplaceholder.dev/posts
REST_API_TABLE_NAME=api_records
REST_API_DATA_PATH=
REST_API_HEADERS_JSON={}
REST_API_QUERY_PARAMS_JSON={}
REST_API_KEY_PARAM=
REST_API_API_KEY=
REST_API_DESCRIPTION=REST API JSON 数据源
REST_API_TIMEOUT=10
REST_API_CACHE_TTL_SECONDS=60
REST_API_SERVICE_MODE=table
```

字段含义：

| 配置项 | 说明 |
| --- | --- |
| REST_API_ENABLED | 是否启用 REST API 数据源 |
| REST_API_NAME | 数据源名称，前端切换和接口调用使用这个值 |
| REST_API_URL | 外部 REST API 地址 |
| REST_API_TABLE_NAME | 映射后的临时表名，LLM 生成 SQL 时会使用 |
| REST_API_DATA_PATH | JSON 响应中对象数组所在路径，例如 `data.items` |
| REST_API_HEADERS_JSON | 请求头 JSON 字符串，可放 Authorization 等 |
| REST_API_QUERY_PARAMS_JSON | 默认 query 参数 JSON 字符串 |
| REST_API_KEY_PARAM | API Key 对应的 query 参数名，例如高德为 `key` |
| REST_API_API_KEY | API Key 值 |
| REST_API_DESCRIPTION | 前端展示用描述 |
| REST_API_TIMEOUT | 请求超时时间，单位秒 |
| REST_API_CACHE_TTL_SECONDS | API 结果短缓存时间，避免重复请求 |
| REST_API_SERVICE_MODE | `table` 表格化查询；`amap_lbs` 高德多服务编排 |

## 4. 可用于实验的公开 API

### 方案 A：JSONPlaceholder posts

适合演示列表数据、字段简单、无需 Token。

```env
REST_API_ENABLED=true
REST_API_NAME=rest_api_demo
REST_API_URL=https://api.jsonplaceholder.dev/posts
REST_API_TABLE_NAME=posts
REST_API_DATA_PATH=
REST_API_HEADERS_JSON={}
REST_API_DESCRIPTION=JSONPlaceholder 文章接口
```

可问：

```text
查询 userId 为 1 的文章数量
查询标题里包含 qui 的文章
按 userId 统计文章数量
```

### 方案 B：REST Countries

适合演示国家、地区、人口等公开数据。

```env
REST_API_ENABLED=true
REST_API_NAME=rest_api_demo
REST_API_URL=https://api.restcountries.com/countries/v5?limit=25&response_fields=names.common,region,population,area
REST_API_TABLE_NAME=countries
REST_API_DATA_PATH=
REST_API_HEADERS_JSON={}
REST_API_DESCRIPTION=REST Countries 国家数据接口
```

可问：

```text
查询人口最多的前 10 个国家
按地区统计国家数量
查询 region 等于 Europe 的国家
```

### 方案 C：Open-Meteo

Open-Meteo 返回的是时间序列结构，通常需要 `REST_API_DATA_PATH=hourly` 后再做进一步适配。当前 MVP 更适合对象数组，所以建议先用 JSONPlaceholder 或 REST Countries 演示。

### 方案 D：高德天气查询

高德 Web 服务 API 需要申请 Web 服务类型 Key，并在 query 参数中传入 `key`。天气查询接口地址为 `/v3/weather/weatherInfo`，必填参数包括 `key` 和城市 `adcode`，`extensions=base` 返回实况天气，`extensions=all` 返回预报天气。

北京东城区示例：

```env
REST_API_ENABLED=true
REST_API_NAME=rest_api_demo
REST_API_URL=https://restapi.amap.com/v3/weather/weatherInfo
REST_API_TABLE_NAME=amap_weather
REST_API_DATA_PATH=lives
REST_API_HEADERS_JSON={}
REST_API_QUERY_PARAMS_JSON={"city":"110101","extensions":"base","output":"JSON"}
REST_API_KEY_PARAM=key
REST_API_API_KEY=你的高德Web服务Key
REST_API_DESCRIPTION=高德地图天气查询接口
REST_API_TIMEOUT=10
REST_API_CACHE_TTL_SECONDS=60
REST_API_SERVICE_MODE=amap_lbs
```

可问：

```text
查询当前配置城市的天气
查询当前配置城市的温度、湿度和风向
查询天气发布时间
```

### 方案 E：高德地理编码

```env
REST_API_ENABLED=true
REST_API_NAME=rest_api_demo
REST_API_URL=https://restapi.amap.com/v3/geocode/geo
REST_API_TABLE_NAME=amap_geocode
REST_API_DATA_PATH=geocodes
REST_API_HEADERS_JSON={}
REST_API_QUERY_PARAMS_JSON={"address":"北京市朝阳区阜通东大街6号","output":"JSON"}
REST_API_KEY_PARAM=key
REST_API_API_KEY=你的高德Web服务Key
REST_API_DESCRIPTION=高德地图地理编码接口
```

可问：

```text
查询这个地址的经纬度
查询这个地址所在城市和行政区
```

### 方案 F：高德关键字搜索

```env
REST_API_ENABLED=true
REST_API_NAME=rest_api_demo
REST_API_URL=https://restapi.amap.com/v3/place/text
REST_API_TABLE_NAME=amap_pois
REST_API_DATA_PATH=pois
REST_API_HEADERS_JSON={}
REST_API_QUERY_PARAMS_JSON={"keywords":"咖啡","city":"北京","offset":"20","page":"1","extensions":"base","output":"JSON"}
REST_API_KEY_PARAM=key
REST_API_API_KEY=你的高德Web服务Key
REST_API_DESCRIPTION=高德地图关键字搜索接口
```

可问：

```text
查询北京咖啡相关地点
按区域统计搜索到的地点数量
查询评分或距离字段最高的地点（如果接口返回这些字段）
```

## 5. 验证步骤

1. 修改后端 `.env`。
2. 重启 FastAPI 后端。
3. 请求数据源列表：

```text
GET /api/v1/data-sources
```

应该看到：

```json
{
  "name": "rest_api_demo",
  "type": "rest_api",
  "status": "connected"
}
```

4. 请求元数据：

```text
GET /api/v1/metadata/rest_api_demo
```

5. 前端进入聊天页，在顶部数据源下拉中选择 REST 接口，然后提问。

## 6. 注意事项

1. 当前 RESTAPIAdapter 只支持 GET 请求。
2. 当前适合返回“对象数组”的 JSON API。
3. 嵌套对象会被展开，例如 `profile.city` 会变成 `profile_city`。
4. 数组字段会保存为 JSON 字符串。
5. 生产环境不要把敏感 Token 写进前端，只能放在后端 `.env`。
