# XiYanSQL-QwenCoder-3B 接入与训练说明

## 当前四模型选择

前端和后端 `/api/v1/models` 现在保留 4 个模型：

| 前端 model_id | 后端实际服务 | 用途 |
| --- | --- | --- |
| `qwen3-coder-next-fp8` | 公司内部 LiteLLM | 默认高质量 NL2SQL 演示 |
| `xiyan-sql-3b-ollama` | `http://127.0.0.1:11434/v1`，模型 `xiyansql-3b` | Ollama 量化版 3B，适合离线答辩演示 |
| `xiyan-sql-3b-finetune` | `http://127.0.0.1:8010/v1`，模型 `XiYanSQL-QwenCoder-3B-2504` | 训练/微调版 3B，用于 QLoRA 前后对比 |
| `qwen3.7-plus` | DashScope | 备用通用模型 |

注意：不要再把 `.env` 里的 `ANTHROPIC_BASE_URL` 改成 Ollama。默认模型应保留公司内网 LiteLLM；Ollama 走 `XIYAN_OLLAMA_*`，微调服务走 `XIYAN_FINETUNE_*`。

## Ollama 版

MiniMax 已经把 GGUF 模型导入 Ollama，当前模型名是：

```text
xiyansql-3b
```

MVP 选择 `xiyan-sql-3b-ollama` 时会调用：

```text
http://127.0.0.1:11434/v1/chat/completions
```

这一路线启动最简单，适合离线演示和答辩。

## 微调服务版

启动 Transformers/OpenAI-compatible 服务：

```powershell
cd Z:\python\Projects\task\nl2sql-presearch
pip install -r requirements-xiyan3b.txt
python scripts\serve_xiyan3b_openai.py --host 127.0.0.1 --port 8010
```

MVP 选择 `xiyan-sql-3b-finetune` 时会调用 `8010`。这一路线用于加载原始模型、QLoRA adapter 或合并后的微调模型。

## 评估

先评估 Ollama 量化版：

```powershell
cd Z:\python\Projects\task\nl2sql-presearch
python training\evaluate_mvp_model.py --model-id xiyan-sql-3b-ollama --cases training\golden_cases.sample.jsonl --report training\xiyan3b_ollama_report.md
```

再评估微调服务版：

```powershell
python training\evaluate_mvp_model.py --model-id xiyan-sql-3b-finetune --cases training\golden_cases.sample.jsonl --report training\xiyan3b_finetune_report.md
```

## 训练

准备 `training\sft_train.jsonl` 后运行：

```powershell
powershell -ExecutionPolicy Bypass -File training\train_qlora_xiyan3b.ps1
```

报告里建议对比四项指标：执行成功率、SQL 结构命中率、平均耗时、P50/P95 耗时。