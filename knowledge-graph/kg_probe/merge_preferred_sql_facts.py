#!/usr/bin/env python3
"""Merge SQL facts with page-code priority and runtime-log fallback."""

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--page-prefix", default="page")
    parser.add_argument("--log-prefix", default="full")
    parser.add_argument("--output-prefix", default="preferred")
    args = parser.parse_args()

    base = Path(args.project_dir)
    page_statements = load(base / f"{args.page_prefix}_sql_statements.json", [])
    page_edges = load(base / f"{args.page_prefix}_dataset_edges.json", [])
    log_statements = load(base / f"{args.log_prefix}_sql_statements.json", [])
    log_edges = load(base / f"{args.log_prefix}_dataset_edges.json", [])

    page_tasks = {item["task_id"] for item in page_statements}
    log_statement_has_write = set()
    for item in log_edges:
        if item["relation"] == "WRITES":
            log_statement_has_write.add(item["from"])

    selected_statements = []
    selected_statements.extend({**item, "preferred_source": "task_page"} for item in page_statements)
    for item in log_statements:
        if item["task_id"] not in page_tasks:
            selected_statements.append({**item, "preferred_source": "runtime_log"})
        elif item["statement_id"] in log_statement_has_write:
            selected_statements.append({**item, "preferred_source": "runtime_log_write_evidence"})
    selected_statement_ids = {item["statement_id"] for item in selected_statements}

    selected_edges = []
    for item in page_edges:
        sid = item["to"] if item["relation"] == "READ_BY" else item["from"]
        if sid in selected_statement_ids:
            selected_edges.append({**item, "preferred_source": "task_page"})
    for item in log_edges:
        sid = item["to"] if item["relation"] == "READ_BY" else item["from"]
        if sid in selected_statement_ids and (
            item["task_id"] not in page_tasks or item["relation"] == "WRITES"
        ):
            selected_edges.append({**item, "preferred_source": "runtime_log"})

    datasets = {}
    for item in selected_edges:
        dataset = item["from"] if item["relation"] == "READ_BY" else item["to"]
        if "." not in dataset:
            layer = "other"
        elif dataset.startswith("odata"):
            layer = "odata"
        elif dataset.startswith("pdata"):
            layer = "pdata"
        elif dataset.startswith("dm_index_n."):
            layer = "dm_index_n"
        elif dataset.startswith("dm") or dataset.startswith("dm."):
            layer = "dm"
        else:
            layer = "other"
        datasets[dataset] = {"dataset": dataset, "layer": layer}

    selected_datasets = sorted(datasets.values(), key=lambda item: item["dataset"])

    write(base / f"{args.output_prefix}_sql_statements.json", selected_statements)
    write(base / f"{args.output_prefix}_dataset_edges.json", selected_edges)
    write(base / f"{args.output_prefix}_datasets.json", selected_datasets)
    write(base / f"{args.output_prefix}_sql_parse_errors.json", [])

    summary = {
        "page_task_count": len(page_tasks),
        "statement_count": len(selected_statements),
        "statement_source_distribution": dict(Counter(item["preferred_source"] for item in selected_statements)),
        "dataset_count": len(selected_datasets),
        "dataset_layer_distribution": dict(Counter(item["layer"] for item in selected_datasets)),
        "edge_count": len(selected_edges),
        "edge_source_distribution": dict(Counter(item["preferred_source"] for item in selected_edges)),
        "edge_type_distribution": dict(Counter(item["relation"] for item in selected_edges)),
    }
    write(base / f"{args.output_prefix}_sql_facts_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
