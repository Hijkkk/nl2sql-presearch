from backend.adapters.graphql_adapter import GraphQLAdapter


class FakeGraphQLResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "data": {
                "countries": [
                    {
                        "code": "CN",
                        "name": "China",
                        "native": "中国",
                        "phone": "86",
                        "capital": "Beijing",
                        "currency": "CNY",
                        "emoji": "🇨🇳",
                        "continent": {"code": "AS", "name": "Asia"},
                        "languages": [{"code": "zh", "name": "Chinese", "native": "中文", "rtl": False}],
                    },
                    {
                        "code": "US",
                        "name": "United States",
                        "native": "United States",
                        "phone": "1",
                        "capital": "Washington D.C.",
                        "currency": "USD,USN,USS",
                        "emoji": "🇺🇸",
                        "continent": {"code": "NA", "name": "North America"},
                        "languages": [{"code": "en", "name": "English", "native": "English", "rtl": False}],
                    },
                    {
                        "code": "JP",
                        "name": "Japan",
                        "native": "日本",
                        "phone": "81",
                        "capital": "Tokyo",
                        "currency": "JPY",
                        "emoji": "🇯🇵",
                        "continent": {"code": "AS", "name": "Asia"},
                        "languages": [{"code": "ja", "name": "Japanese", "native": "日本語", "rtl": False}],
                    },
                ]
            }
        }


class FakeGraphQLClient:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        assert kwargs["json"]["query"]
        return FakeGraphQLResponse()


def make_adapter(client):
    return GraphQLAdapter(
        name="countries_graphql",
        endpoint="https://countries.trevorblades.com/graphql",
        table_name="countries",
        http_client=client,
    )


def test_graphql_adapter_exposes_countries_metadata():
    adapter = make_adapter(FakeGraphQLClient())

    metadata = adapter.get_metadata()

    assert metadata["total_tables"] == 5
    tables_by_name = {table["name"]: table for table in metadata["tables"]}
    assert {
        "countries",
        "country_currencies",
        "country_languages",
        "dict_continent",
        "v_country_profile",
    } <= set(tables_by_name)
    column_names = {column["name"] for column in tables_by_name["countries"]["columns"]}
    assert {"code", "name", "capital", "currency", "continent_name", "language_names"} <= column_names
    assert tables_by_name["country_currencies"]["primary_key"] == ["country_code", "currency_code"]


def test_graphql_adapter_executes_sql_over_virtual_table_and_reuses_cache():
    client = FakeGraphQLClient()
    adapter = make_adapter(client)

    rows, columns = adapter.execute_query(
        "SELECT continent_name, COUNT(*) AS country_count "
        "FROM countries GROUP BY continent_name ORDER BY country_count DESC"
    )

    assert columns == ["continent_name", "country_count"]
    assert rows[0] == {"continent_name": "Asia", "country_count": 2}
    assert client.calls == 1

    adapter.get_metadata()
    assert client.calls == 1


def test_graphql_adapter_exposes_split_tables_and_profile_view():
    adapter = make_adapter(FakeGraphQLClient())

    rows, columns = adapter.execute_query(
        "SELECT currency_code FROM country_currencies "
        "WHERE country_code = 'US' ORDER BY currency_code"
    )

    assert columns == ["currency_code"]
    assert rows == [{"currency_code": "USD"}, {"currency_code": "USN"}, {"currency_code": "USS"}]

    rows, columns = adapter.execute_query(
        "SELECT country_code, currency_codes, language_names "
        "FROM v_country_profile WHERE country_code = 'CN'"
    )

    assert columns == ["country_code", "currency_codes", "language_names"]
    assert rows == [{"country_code": "CN", "currency_codes": "CNY", "language_names": "Chinese"}]
