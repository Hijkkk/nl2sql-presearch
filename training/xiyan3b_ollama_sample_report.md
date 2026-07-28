# NL2SQL MVP 模型评测报告 - xiyan-sql-3b-ollama

- 用例数：4
- 通过数：3
- 通过率：75.00%
- 平均耗时：5.069s
- P50：2.281s
- P95：14.825s

| ID | 数据源 | 通过 | 行数 | 耗时(s) | 错误 |
| --- | --- | --- | ---: | ---: | --- |
| sqlite_dept_count | sqlite_demo | True | 3 | 2.142 |  |
| postgres_apple_latest_close | postgres_stock | False | 0 | 14.825 |  |
| hadoop_city_gmv | hive_hadoop_demo | True | 1 | 1.027 |  |
| graphql_continent_count | countries_graphql | True | 7 | 2.281 |  |
