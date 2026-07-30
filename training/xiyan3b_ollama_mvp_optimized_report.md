# NL2SQL MVP 模型评测报告 - xiyan-sql-3b-ollama

- 用例数：33
- 通过数：33
- 通过率：100.00%
- 平均耗时：2.427s
- P50：1.708s
- P95：4.354s

| ID | 数据源 | 通过 | 行数 | 耗时(s) | 错误 |
| --- | --- | --- | ---: | ---: | --- |
| sqlite_dept_count | sqlite_demo | True | 3 | 1.716 |  |
| sqlite_avg_salary_by_dept | sqlite_demo | True | 3 | 0.879 |  |
| sqlite_high_salary_employees | sqlite_demo | True | 6 | 1.629 |  |
| sqlite_department_sales | sqlite_demo | True | 2 | 1.293 |  |
| mysql_address_type_count | mysql_police_address | True | 1 | 1.054 |  |
| mysql_district_address_count | mysql_police_address | True | 1 | 1.46 |  |
| mysql_person_current_address | mysql_police_address | True | 2 | 2.881 |  |
| mysql_address_alias_search | mysql_police_address | True | 0 | 1.616 |  |
| postgres_exchange_stock_count | postgres_stock | True | 2 | 1.203 |  |
| postgres_top_volume | postgres_stock | True | 10 | 1.3 |  |
| postgres_apple_latest_close | postgres_stock | True | 1 | 1.518 |  |
| postgres_leveraged_etf | postgres_stock | True | 2 | 4.354 |  |
| postgres_inverse_etf | postgres_stock | True | 1 | 1.346 |  |
| postgres_sic_parent | postgres_stock | True | 1 | 2.459 |  |
| gauss_city_customer_count | gauss_ecommerce | True | 5 | 1.1 |  |
| gauss_top_customers_amount | gauss_ecommerce | True | 5 | 1.965 |  |
| gauss_iphone_customers | gauss_ecommerce | True | 0 | 1.564 |  |
| gauss_monthly_2024_sales | gauss_ecommerce | True | 5 | 1.562 |  |
| dameng_city_customer_count | dameng_ecommerce | True | 5 | 4.314 |  |
| dameng_top_customers_amount | dameng_ecommerce | True | 5 | 9.072 |  |
| dameng_iphone_customers | dameng_ecommerce | True | 0 | 3.56 |  |
| dameng_monthly_2024_sales | dameng_ecommerce | True | 5 | 4.017 |  |
| hadoop_city_gmv | hive_hadoop_demo | True | 5 | 1.708 |  |
| hadoop_vip_gmv | hive_hadoop_demo | True | 1 | 1.614 |  |
| hadoop_brand_monthly_sales | hive_hadoop_demo | True | 29 | 7.286 |  |
| hadoop_region_order_amount | hive_hadoop_demo | True | 7 | 3.891 |  |
| rest_amap_weather | rest_api_demo | True | 1 | 1.998 |  |
| rest_amap_distance | rest_api_demo | True | 1 | 1.707 |  |
| rest_amap_geocode | rest_api_demo | True | 1 | 0.257 |  |
| graphql_continent_count | countries_graphql | True | 7 | 3.539 |  |
| graphql_asia_capitals | countries_graphql | True | 52 | 2.173 |  |
| graphql_english_usd | countries_graphql | True | 14 | 1.941 |  |
| graphql_currency_country_count | countries_graphql | True | 160 | 2.12 |  |
