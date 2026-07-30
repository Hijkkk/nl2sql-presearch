# NL2SQL 微调数据与 QLoRA 方案（2026-07-29）

## 当前决策

暂不直接启动 QLoRA。

理由：最新优化 baseline 在 33 条 MVP golden cases 上达到 100% 通过率。现阶段继续微调的边际收益不明确，且直接用模型输出构造训练集存在把错误固化进 LoRA 的风险。

## 何时创建 `training/sft_train.jsonl`

满足以下条件后再创建正式训练集：

1. 新增独立评估集，建议至少 80 到 120 条，覆盖 8 个数据源。
2. 每条样本的目标 SQL 经过人工或确定性脚本验证。
3. 训练集和评估集严格分离，失败用例不能原样同时进入训练和最终评估。
4. 每个数据源保留足够多的 JOIN、聚合、日期、模糊匹配和业务口径样本。

## 推荐 JSONL 格式

每行一条训练样本：

```json
{"messages":[{"role":"system","content":"你是专业 SQL 生成助手。只输出一条只读 SQL。"},{"role":"user","content":"【数据源】mysql_police_address\n【方言】MySQL\n【Schema】...\n【问题】查询包含人民路的地址别名及对应标准地址。"},{"role":"assistant","content":"SELECT aa.alias_name AS 地址别名, sa.full_address AS 标准地址 FROM addr_alias aa JOIN addr_standard_address sa ON aa.std_address_code = sa.std_address_code WHERE aa.alias_name LIKE '%人民路%' LIMIT 100;"}],"metadata":{"data_source":"mysql_police_address","category":"join_filter","verified":true}}
```

关键要求：

- assistant 内容只放 SQL，不放解释、Markdown 或 thought。
- user 内容只包含必要 schema、数据源、方言和问题，避免塞入无关表。
- metadata 标注 `data_source`、`category`、`difficulty`、`verified`，便于抽样和质量审计。
- 不写入数据库密码、API Key、Token、私有 Base URL。

## 样本来源优先级

1. 当前 MVP golden cases 的人工校验 SQL。
2. 新增业务口径用例，例如地址别名、当前住址、股票中文名、ETF 属性、SIC 行业树。
3. baseline 或人工测试失败后修复成功的样本。
4. 真实用户问题，经人工确认后加入。

不建议使用：

- 未执行验证的模型输出。
- 只靠关键词通过但语义不完整的 SQL。
- 包含敏感凭据或过长全量 schema 的 prompt。

## QLoRA 执行方案

前置条件满足后，使用现有入口：

```powershell
cd Z:\python\Projects\task\nl2sql-presearch
pip install "ms-swift[llm]"
.\training\train_qlora_xiyan3b.ps1
```

训练前必须确认：

- 使用 Hugging Face safetensors 原始模型，不训练 GGUF / Ollama 量化文件。
- 输出目录为 `outputs\xiyan3b-qlora`。
- 显卡显存、CUDA、bf16/fp16 能力满足训练脚本要求。
- `XIYAN_FINETUNE_*` 用于微调服务，不和 `XIYAN_OLLAMA_*` 混用。

## 评估闭环

1. 固定同一份独立评估集。
2. 跑基座模型 `xiyan-sql-3b-ollama` 或同等未微调部署。
3. 跑微调服务 `xiyan-sql-3b-finetune`。
4. 比较通过率、SQL 执行成功率、语义正确率、P50/P95、失败类型。
5. 只有在独立集上稳定提升时，才把 LoRA 作为演示或默认候选。

## 当前建议

下一阶段优先做两件事：

1. 把 MVP golden cases 扩展到 80 到 120 条，并提升断言强度，例如检查必要字段、过滤条件和分组字段。
2. 基于新增失败样本再决定是否创建正式 `training/sft_train.jsonl` 并启动 QLoRA。
