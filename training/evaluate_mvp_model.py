"""Evaluate a model through the NL2SQL MVP /api/v1/chat endpoint.

Example:
    python training\evaluate_mvp_model.py --base-url http://127.0.0.1:8002 --model-id xiyan-sql-3b-ollama --cases training\golden_cases.sample.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def get_token(client: httpx.Client, username: str, password: str) -> str:
    payload = {"username": username, "password": password}
    try:
        resp = client.post("/api/user/login", json=payload)
        if resp.status_code >= 400:
            resp = client.post("/api/user/register", json=payload)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"auth failed: {exc.response.status_code} {exc.response.text}") from exc
    data = resp.json().get("data") or {}
    token = data.get("token")
    if not token:
        raise RuntimeError(f"auth response missing token: {resp.text}")
    return token


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p)))
    return ordered[index]


def evaluate(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cases = load_cases(Path(args.cases))
    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        token = get_token(client, args.username, args.password)
        headers = {"Authorization": f"Bearer {token}"}
        rows: list[dict[str, Any]] = []
        for case in cases:
            started = time.perf_counter()
            resp = client.post(
                "/api/v1/chat",
                headers=headers,
                json={
                    "question": case["question"],
                    "data_source": case["data_source"],
                    "model_id": args.model_id,
                    "model_config": {"temperature": args.temperature, "top_p": args.top_p, "max_tokens": args.max_tokens},
                },
            )
            wall_time = time.perf_counter() - started
            ok_http = resp.status_code < 400
            body = resp.json() if ok_http else {"success": False, "error": resp.text}
            sql = body.get("sql") or ""
            expected_parts = case.get("expected_sql_contains", [])
            sql_contains_ok = all(part.lower() in sql.lower() for part in expected_parts)
            min_rows = int(case.get("min_rows", 0))
            row_count = int(body.get("row_count") or 0)
            passed = bool(body.get("success")) == bool(case.get("expected_success", True)) and sql_contains_ok and row_count >= min_rows
            rows.append({
                "id": case["id"],
                "data_source": case["data_source"],
                "question": case["question"],
                "passed": passed,
                "success": bool(body.get("success")),
                "row_count": row_count,
                "wall_time": round(wall_time, 3),
                "sql_contains_ok": sql_contains_ok,
                "sql": sql,
                "error": body.get("error"),
            })
    times = [r["wall_time"] for r in rows]
    summary = {
        "model_id": args.model_id,
        "total": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "pass_rate": round(sum(1 for r in rows if r["passed"]) / len(rows), 4) if rows else 0,
        "avg_wall_time": round(statistics.mean(times), 3) if times else 0,
        "p50_wall_time": round(percentile(times, 0.5), 3),
        "p95_wall_time": round(percentile(times, 0.95), 3),
    }
    return rows, summary


def write_report(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        f"# NL2SQL MVP 模型评测报告 - {summary['model_id']}",
        "",
        f"- 用例数：{summary['total']}",
        f"- 通过数：{summary['passed']}",
        f"- 通过率：{summary['pass_rate']:.2%}",
        f"- 平均耗时：{summary['avg_wall_time']}s",
        f"- P50：{summary['p50_wall_time']}s",
        f"- P95：{summary['p95_wall_time']}s",
        "",
        "| ID | 数据源 | 通过 | 行数 | 耗时(s) | 错误 |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for row in rows:
        error = (row.get("error") or "").replace("|", "\\|")[:120]
        lines.append(f"| {row['id']} | {row['data_source']} | {row['passed']} | {row['row_count']} | {row['wall_time']} | {error} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json_report(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    path.write_text(
        json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_csv_report(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "id",
        "data_source",
        "question",
        "passed",
        "success",
        "row_count",
        "wall_time",
        "sql_contains_ok",
        "sql",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--model-id", default="xiyan-sql-3b-ollama")
    parser.add_argument("--cases", default="training/golden_cases.sample.jsonl")
    parser.add_argument("--report", default="training/evaluation_report.md")
    parser.add_argument("--json-report", default="")
    parser.add_argument("--csv-report", default="")
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default="demo123456")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    rows, summary = evaluate(args)
    report_path = Path(args.report)
    write_report(report_path, rows, summary)
    json_report = Path(args.json_report) if args.json_report else report_path.with_suffix(".json")
    csv_report = Path(args.csv_report) if args.csv_report else report_path.with_suffix(".csv")
    write_json_report(json_report, rows, summary)
    write_csv_report(csv_report, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
