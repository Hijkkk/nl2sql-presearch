# NL2SQL MVP 优化 Baseline 测试报告（2026-07-29）

## 结论

在标准后端 `http://127.0.0.1:8002`、模型 `xiyan-sql-3b-ollama`、本地 PostgreSQL 数据源 `postgres_stock` 环境下，33 条 MVP golden cases 全部通过。

- 用例数：33
- 通过数：33
- 通过率：100.00%
- 平均耗时：2.427s
- P50：1.708s
- P95：4.354s

报告文件：

- `training/xiyan3b_ollama_mvp_optimized_report.md`
- `training/xiyan3b_ollama_mvp_optimized_report.json`
- `training/xiyan3b_ollama_mvp_optimized_report.csv`

## 验证环境

- 后端：`Z:\python\Projects\task\nl2sql-presearch`
- 前端：`Z:\python\Projects\task\fronted`
- 后端端口：`http://127.0.0.1:8002`
- 评估模型：`xiyan-sql-3b-ollama`
- PostgreSQL 容器：`pg-local`
- PostgreSQL 宿主端口：25432
- PostgreSQL 数据库：`stock_market`
- PostgreSQL 数据源名：`postgres_stock`

敏感凭据只从后端 `.env` 读取，未写入本报告。

## 本轮修正

1. XiYanSQL 专用 prompt 增加数据源专属参考信息。
2. `hive_hadoop_demo` 明确本地演示由 SQLite 执行 CSV，按月统计使用 `substr(event_date, 1, 7)`，避免生成 `TO_DATE` / `DATE_FORMAT`。
3. `mysql_police_address` 地址别名示例修正为真实业务键：
   - `addr_alias.std_address_code`
   - `addr_standard_address.std_address_code`
4. `scripts/view_audit.py` 增加 UTF-8 stdout/stderr 输出保护，避免 Windows GBK 控制台打印多语言审计样本时报错。

## 分数据源结果

| 数据源 | 用例数 | 通过数 | 平均耗时 |
| --- | ---: | ---: | ---: |
| sqlite_demo | 4 | 4 | 1.379s |
| mysql_police_address | 4 | 4 | 1.753s |
| postgres_stock | 6 | 6 | 2.030s |
| gauss_ecommerce | 4 | 4 | 1.548s |
| dameng_ecommerce | 4 | 4 | 5.241s |
| hive_hadoop_demo | 4 | 4 | 3.625s |
| rest_api_demo | 3 | 3 | 1.321s |
| countries_graphql | 4 | 4 | 2.443s |

## 执行过的验证

```powershell
cd Z:\python\Projects\task\fronted
npm run build
```

结果：通过。

```powershell
cd Z:\python\Projects\task\nl2sql-presearch
python -m pytest -q
```

结果：`48 passed`。

```powershell
python -m py_compile backend\nl2sql\prompt_builder.py backend\nl2sql\sql_generator.py scripts\view_audit.py
```

结果：通过。

```powershell
python training\evaluate_mvp_model.py `
  --base-url http://127.0.0.1:8002 `
  --model-id xiyan-sql-3b-ollama `
  --cases training\golden_cases.mvp.jsonl `
  --report training\xiyan3b_ollama_mvp_optimized_report.md `
  --username codex_eval_mvp `
  --password demo123456 `
  --timeout 240
```

结果：33/33 通过。

## 审计验证

当天审计路径：

```text
data/audit/2026-07-29/audit_2026-07-29.db
```

最新 33 条 `xiyan-sql-3b-ollama` 评估记录检查结果：

- `generated_sql`：33/33 非空
- `executed_sql`：33/33 非空
- `result_sample_json`：33/33 非空
- `stage_timings_json`：33/33 非空
- 完整五阶段耗时：30/33
- `raw_model_output`：30/33 非空

说明：3 条 `rest_api_demo` 用例走高德 REST 服务编排，不经过 SQL 模型生成阶段，因此没有 `raw_model_output` 和 SQL 生成阶段耗时；其审计仍记录 API 执行动作、结果列、结果样本和总耗时。

## 微调判断

当前 baseline 已达到 MVP golden cases 100% 通过。现阶段不建议直接启动 QLoRA；更合理的下一步是扩大独立评估集并收集真实失败样本。如果新增评估集出现稳定错误，再基于人工校验 SQL 构造 `training/sft_train.jsonl`。
