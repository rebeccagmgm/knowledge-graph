#!/usr/bin/env python3
"""Generate task and dataset summaries for query-layer context."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


def compact_props(node: dict) -> dict:
    return node.get("properties", {})


def post_json(base_url: str, api_key: str, model: str, messages: list[dict], timeout: int, max_tokens: int) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    text = raw["choices"][0]["message"]["content"]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1] if start >= 0 and end > start else text)


def graph_indexes(nodes: list[dict], edges: list[dict]):
    by_id = {node["id"]: node for node in nodes}
    by_from = defaultdict(list)
    by_to = defaultdict(list)
    for edge in edges:
        by_from[edge.get("from")].append(edge)
        by_to[edge.get("to")].append(edge)
    return by_id, by_from, by_to


def mock_task_summary(task: dict, by_id: dict, by_from: dict) -> dict:
    props = compact_props(task)
    produced, consumed, sql_ids = [], [], []
    for edge in by_from.get(task["id"], []):
        target = by_id.get(edge.get("to"))
        if not target:
            continue
        labels = set(target.get("labels", []))
        if edge.get("type") == "PRODUCES" and "Dataset" in labels:
            produced.append(compact_props(target).get("name"))
        elif edge.get("type") == "CONSUMES" and "Dataset" in labels:
            consumed.append(compact_props(target).get("name"))
        elif edge.get("type") == "EMITS_SQL":
            sql_ids.append(target.get("id"))
    return {
        "task_node_id": task["id"],
        "task_id": props.get("task_id"),
        "task_name": props.get("task_name") or props.get("name"),
        "summary": f"任务读取{len(consumed)}张表，产出{len(produced)}张表，关联{len(sql_ids)}条SQL证据。",
        "business_logic": "mock 模式基于图结构生成摘要；真实业务逻辑可切换 LLM provider 生成。",
        "inputs": consumed[:30],
        "outputs": produced[:30],
        "sql_statement_count": len(sql_ids),
        "confidence": "medium" if sql_ids or produced else "low",
    }


def mock_dataset_summary(dataset: dict, by_id: dict, by_from: dict, by_to: dict) -> dict:
    props = compact_props(dataset)
    producers, consumers, columns, metrics = [], [], [], []
    for edge in by_to.get(dataset["id"], []):
        source = by_id.get(edge.get("from"))
        if source and edge.get("type") == "PRODUCES":
            producers.append(compact_props(source).get("task_id"))
        if source and edge.get("type") == "STORED_IN":
            metrics.append(compact_props(source).get("metric_id"))
    for edge in by_from.get(dataset["id"], []):
        target = by_id.get(edge.get("to"))
        if target and edge.get("type") == "HAS_COLUMN":
            columns.append(compact_props(target).get("name"))
    for edge in by_to.get(dataset["id"], []):
        source = by_id.get(edge.get("from"))
        if source and edge.get("type") == "CONSUMES":
            consumers.append(compact_props(source).get("task_id"))
    return {
        "dataset_id": dataset["id"],
        "dataset": props.get("name"),
        "layer": props.get("layer"),
        "summary": f"表位于{props.get('layer') or '未知'}层，包含{len(columns)}个字段，由{len(producers)}个任务生产，被{len(consumers)}个任务消费，关联{len(metrics)}个指标。",
        "business_meaning": props.get("comment") or "mock 模式未生成业务含义。",
        "producer_task_ids": producers[:30],
        "consumer_task_ids": consumers[:30],
        "metric_ids": metrics[:30],
        "sample_columns": columns[:50],
        "confidence": "medium" if columns or producers else "low",
    }


def llm_summary(kind: str, payload: dict, args) -> dict:
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("codex_ds_API_KEY") or os.environ.get("CODEX_DS_API_KEY")
    if not api_key:
        raise SystemExit("LLM_API_KEY or OPENAI_API_KEY is required for openai-compatible provider")
    messages = [
        {"role": "system", "content": "你是资深数据研发专家。请基于证据生成简洁、可追溯的数据研发摘要，只输出 JSON。"},
        {"role": "user", "content": f"资产类型：{kind}\n证据 JSON:\n{json.dumps(payload, ensure_ascii=False)}\n\n请输出 JSON，字段包括 summary、business_logic 或 business_meaning、confidence、uncertainties。"},
    ]
    parsed = post_json(args.base_url, api_key, args.model, messages, args.timeout, args.max_tokens)
    return {**payload, **parsed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--provider", choices=["mock", "openai-compatible"], default="mock")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "deepseek-v4-pro"))
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-tokens", type=int, default=2048)
    args = parser.parse_args()

    base = Path(args.project_dir)
    nodes = load_jsonl(base / f"{args.prefix}_graph_nodes.jsonl")
    edges = load_jsonl(base / f"{args.prefix}_graph_edges.jsonl")
    by_id, by_from, by_to = graph_indexes(nodes, edges)
    tasks = [node for node in nodes if "ScheduleTask" in node.get("labels", [])]
    datasets = [node for node in nodes if "Dataset" in node.get("labels", [])]
    if args.limit:
        tasks = tasks[: args.limit]
        datasets = datasets[: args.limit]

    task_rows = []
    for task in tasks:
        payload = mock_task_summary(task, by_id, by_from)
        task_rows.append(llm_summary("task", payload, args) if args.provider != "mock" else payload)
    dataset_rows = []
    for dataset in datasets:
        payload = mock_dataset_summary(dataset, by_id, by_from, by_to)
        dataset_rows.append(llm_summary("dataset", payload, args) if args.provider != "mock" else payload)

    generated_at = datetime.now().isoformat(timespec="seconds")
    for row in task_rows + dataset_rows:
        row["provider"] = args.provider
        row["model"] = args.model if args.provider != "mock" else "mock"
        row["generated_at"] = generated_at

    write_jsonl(base / "llm" / "task_summaries.jsonl", task_rows)
    write_jsonl(base / "llm" / "dataset_summaries.jsonl", dataset_rows)
    print(json.dumps({"task_summary_count": len(task_rows), "dataset_summary_count": len(dataset_rows), "generated_at": generated_at}, ensure_ascii=False))


if __name__ == "__main__":
    main()
