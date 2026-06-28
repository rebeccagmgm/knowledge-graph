#!/usr/bin/env python3
"""Merge SQL facts by task-type strategy.

Current rule:
- hiveTask / hiveTask-2.0: runtime log facts
- everything else: task page facts
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def layer_of(dataset: str) -> str:
    if dataset.startswith("odata"):
        return "odata"
    if dataset.startswith("pdata"):
        return "pdata"
    if dataset.startswith("dm_index_n."):
        return "dm_index_n"
    if dataset.startswith("dm") or dataset.startswith("dm."):
        return "dm"
    return "other"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--page-prefix", default="page")
    parser.add_argument("--log-prefix", default="hive_log")
    parser.add_argument("--output-prefix", default="strategy")
    parser.add_argument("--log-task-types", default="hiveTask,hiveTask-2.0")
    args = parser.parse_args()

    base = Path(args.project_dir)
    details = load(base / "task_details.json", [])
    log_task_types = {item.strip() for item in args.log_task_types.split(",") if item.strip()}
    type_by_task = {item["task_id"]: item.get("task_type", "") for item in details}
    log_task_ids = {task_id for task_id, task_type in type_by_task.items() if task_type in log_task_types}

    page_statements = load(base / f"{args.page_prefix}_sql_statements.json", [])
    page_edges = load(base / f"{args.page_prefix}_dataset_edges.json", [])
    page_errors = load(base / f"{args.page_prefix}_sql_parse_errors.json", [])

    log_statements = load(base / f"{args.log_prefix}_sql_statements.json", [])
    log_edges = load(base / f"{args.log_prefix}_dataset_edges.json", [])
    log_errors = load(base / f"{args.log_prefix}_sql_parse_errors.json", [])

    selected_statements = [
        {**item, "strategy_source": "runtime_log", "strategy_reason": "hive_task_log"}
        for item in log_statements
        if item["task_id"] in log_task_ids
    ]
    selected_statements.extend(
        {**item, "strategy_source": "task_page", "strategy_reason": "non_hive_task_page"}
        for item in page_statements
        if item["task_id"] not in log_task_ids
    )
    selected_statement_ids = {item["statement_id"] for item in selected_statements}

    selected_edges = []
    for item in log_edges:
        sid = item["to"] if item["relation"] == "READ_BY" else item["from"]
        if item["task_id"] in log_task_ids and sid in selected_statement_ids:
            selected_edges.append({**item, "strategy_source": "runtime_log"})
    for item in page_edges:
        sid = item["to"] if item["relation"] == "READ_BY" else item["from"]
        if item["task_id"] not in log_task_ids and sid in selected_statement_ids:
            selected_edges.append({**item, "strategy_source": "task_page"})

    datasets = {}
    for item in selected_edges:
        dataset = item["from"] if item["relation"] == "READ_BY" else item["to"]
        datasets[dataset] = {"dataset": dataset, "layer": layer_of(dataset)}

    selected_errors = [
        {**item, "strategy_source": "runtime_log"}
        for item in log_errors
        if item.get("task_id") in log_task_ids
    ]
    selected_errors.extend(
        {**item, "strategy_source": "task_page"}
        for item in page_errors
        if item.get("task_id") not in log_task_ids
    )

    selected_datasets = sorted(datasets.values(), key=lambda item: item["dataset"])
    write(base / f"{args.output_prefix}_sql_statements.json", selected_statements)
    write(base / f"{args.output_prefix}_dataset_edges.json", selected_edges)
    write(base / f"{args.output_prefix}_datasets.json", selected_datasets)
    write(base / f"{args.output_prefix}_sql_parse_errors.json", selected_errors)

    summary = {
        "log_task_type_count": len(log_task_types),
        "log_task_count": len(log_task_ids),
        "statement_count": len(selected_statements),
        "statement_source_distribution": dict(Counter(item["strategy_source"] for item in selected_statements)),
        "dataset_count": len(selected_datasets),
        "dataset_layer_distribution": dict(Counter(item["layer"] for item in selected_datasets)),
        "edge_count": len(selected_edges),
        "edge_source_distribution": dict(Counter(item["strategy_source"] for item in selected_edges)),
        "edge_type_distribution": dict(Counter(item["relation"] for item in selected_edges)),
        "parse_error_count": len(selected_errors),
    }
    write(base / f"{args.output_prefix}_sql_facts_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
