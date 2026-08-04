"""
以中文打印最近的 NL2SQL 审计记录。

view_audit.py 用法
查看今天：
E:\SoftWare\anaconda\envs\fastapi_project\python.exe scripts\view_audit.py
查看指定日期：
E:\SoftWare\anaconda\envs\fastapi_project\python.exe scripts\view_audit.py --date 2026-07-28
限制条数：
E:\SoftWare\anaconda\envs\fastapi_project\python.exe scripts\view_audit.py --date 2026-07-31 --limit 1
列出已有日期：
E:\SoftWare\anaconda\envs\fastapi_project\python.exe scripts\view_audit.py --list-dates
旧审计仍在：
data/audit.db
新审计从重启后开始写入：
data/audit/YYYY-MM-DD/audit_YYYY-MM-DD.db
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.audit import audit_db_path, audit_root_dir, init_audit_db


SEPARATOR = "=" * 88
STAGE_LABELS = {
    "metadata": "元数据读取与构建",
    "sql_generation": "大模型生成 SQL",
    "database": "数据库执行 SQL",
    "result_summary": "大模型生成结果摘要",
    "total": "端到端总耗时",
}


def parse_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def show_stage_timings(raw: str | None) -> None:
    timings = parse_json(raw, {})
    if not timings:
        print("阶段耗时：无（该记录由旧版本服务写入）")
        return

    print("阶段耗时：")
    for key, label in STAGE_LABELS.items():
        seconds = timings.get(key)
        if seconds is not None:
            print(f"  - {label}：{float(seconds):.3f} 秒")


def list_audit_dates() -> None:
    root = audit_root_dir()
    if not root.exists():
        print(f"暂无按天审计目录：{root.resolve()}")
        return
    dates = sorted(path.name for path in root.iterdir() if path.is_dir())
    if not dates:
        print(f"暂无按天审计目录：{root.resolve()}")
        return
    print("可查询日期：")
    for item in dates:
        print(f"  - {item}")


def main() -> None:
    # ================= 硬编码配置区域 =================
    AUDIT_DATE = "2026-07-31"  # 修改这里查看指定日期
    LIMIT = 1                 # 显示最近 N 条
    # ================================================

    parser = argparse.ArgumentParser(description="查看 NL2SQL 审计记录")
    parser.add_argument("--date", default=None, help="审计日期，格式 YYYY-MM-DD；默认今天")
    parser.add_argument("--limit", type=int, default=20, help="显示最近 N 条记录，默认 20")
    parser.add_argument("--list-dates", action="store_true", help="列出已有审计日期")
    args = parser.parse_args()

    if args.list_dates:
        list_audit_dates()
        return

    # 优先使用命令行参数，其次使用硬编码值
    audit_date = args.date or AUDIT_DATE
    limit = args.limit or LIMIT
    print(f"DEBUG: audit_date={audit_date}, limit={limit}")  # 调试输出

    init_audit_db(audit_date)
    db_path = audit_db_path(audit_date)
    if not db_path.exists():
        print(f"未找到审计数据库：{db_path.resolve()}")
        return

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT timestamp, question, data_source, status, execution_time, row_count,
                      rag_enabled, rag_top_score, selected_tables_json, query_guard_passed,
                      prompt_token_estimate, stage_timings_json, generated_sql, executed_sql,
                       error_message, model_id, raw_model_output, llm_thought, prompt_template,
                      generation_cache_hit, correction_attempted, corrected_sql,
                      result_columns_json, result_sample_json, result_truncated
               FROM audit_logs ORDER BY id DESC LIMIT ?""",
            (max(1, limit),),
        ).fetchall()

    if not rows:
        print("暂无审计记录。")
        return

    for index, row in enumerate(rows, start=1):
        tables = parse_json(row["selected_tables_json"], [])
        result_columns = parse_json(row["result_columns_json"], [])
        result_sample = parse_json(row["result_sample_json"], [])
        print(SEPARATOR)
        print(f"记录 #{index}")
        print(f"请求时间：{row['timestamp']}")
        print(f"执行状态：{row['status']}")
        print(f"当前数据源：{row['data_source']}")
        print(f"模型：{row['model_id'] or '未记录'}")
        print(f"用户问题：{row['question'] or '未记录'}")
        print(f"总耗时：{float(row['execution_time'] or 0):.3f} 秒")
        print(f"返回行数：{row['row_count']}")
        print(f"QueryGuard 安全校验：{'通过' if row['query_guard_passed'] else '未通过或未执行'}")
        print(f"估算 Prompt Token 数：{row['prompt_token_estimate'] or 0}")
        print(f"SQL 生成缓存：{'命中' if row['generation_cache_hit'] else '未命中或未记录'}")
        print(f"相关表：{', '.join(tables) if tables else '未记录'}")
        print(f"RAG：{'启用（历史记录）' if row['rag_enabled'] else '未启用'}")
        if row["rag_top_score"] is not None:
            print(f"RAG 最高相似度：{float(row['rag_top_score']):.4f}")
        show_stage_timings(row["stage_timings_json"])
        print("生成 SQL：")
        print(row["generated_sql"] or "未生成")
        if row["executed_sql"] and row["executed_sql"] != row["generated_sql"]:
            print("执行 SQL：")
            print(row["executed_sql"])
        if row["llm_thought"]:
            print("模型解释/提取 thought：")
            print(row["llm_thought"][:1000])
        if row["prompt_template"]:
            print("SQL 提示词：")
            print(row["prompt_template"][:10000])
        if row["raw_model_output"]:
            print("模型原始输出：")
            print(row["raw_model_output"][:1000])
        if row["correction_attempted"]:
            print(f"自修复：已尝试，修复 SQL：{row['corrected_sql'] or '未生成'}")
        if result_columns:
            print(f"结果列：{', '.join(str(item) for item in result_columns)}")
        if result_sample:
            print(f"结果样本（最多显示 {len(result_sample)} 行）：")
            print(json.dumps(result_sample, ensure_ascii=False, indent=2, default=str)[:3000])
            if row["result_truncated"]:
                print("结果样本：已截断，完整结果未写入审计以避免审计库过大。")
        if row["error_message"]:
            print(f"错误信息：{row['error_message']}")
    print(SEPARATOR)


if __name__ == "__main__":
    main()
