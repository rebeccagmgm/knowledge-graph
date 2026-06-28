#!/usr/bin/env python3
"""Build LLM evidence bundles from graph facts."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import defaultdict
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return html.unescape(text).strip()


def stable_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def truncate(value: str, max_chars: int) -> tuple[str, bool]:
    value = value or ""
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def sql_text(sql_node: dict, max_chars: int) -> dict:
    props = sql_node.get("properties", {})
    path = props.get("statement_path")
    text = ""
    readable = False
    if path and Path(path).exists():
        text = Path(path).read_text(errors="replace")
        readable = True
    clipped, was_truncated = truncate(text, max_chars)
    return {
        "statement_id": props.get("statement_id"),
        "task_id": props.get("task_id"),
        "statement_path": path,
        "parse_ok": props.get("parse_ok"),
        "read_dataset_count": props.get("read_dataset_count"),
        "write_dataset_count": props.get("write_dataset_count"),
        "sql_text": clipped,
        "sql_text_truncated": was_truncated,
        "sql_text_readable": readable,
    }


def compact_dataset(item: dict) -> dict:
    props = item.get("properties", item)
    return {
        "id": item.get("id") or props.get("id"),
        "name": props.get("name"),
        "layer": props.get("layer"),
        "db_name": props.get("db_name"),
        "qualified_name": props.get("qualified_name"),
        "comment": props.get("comment"),
        "source_type": props.get("source_type"),
    }


def compact_column(item: dict) -> dict:
    props = item.get("properties", item)
    return {
        "id": item.get("id") or props.get("id"),
        "dataset": props.get("dataset"),
        "name": props.get("name"),
        "source_system": props.get("source_system"),
        "source_type": props.get("source_type"),
    }


def compact_task(item: dict) -> dict:
    props = item.get("properties", item)
    return {
        "id": item.get("id") or props.get("id"),
        "task_id": props.get("task_id"),
        "name": props.get("name"),
        "task_type": props.get("task_type"),
        "layer": props.get("layer"),
        "owner": props.get("owner"),
    }


def compact_definition(item: dict) -> dict:
    props = item.get("properties", item)
    return {
        "id": item.get("id") or props.get("id"),
        "metric_id": props.get("metric_id"),
        "definition": props.get("definition"),
        "formula": props.get("formula"),
        "business_meaning": props.get("business_meaning"),
        "source_system": props.get("source_system"),
    }


def edge_index(edges: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]]]:
    by_from: dict[str, list[dict]] = defaultdict(list)
    by_to: dict[str, list[dict]] = defaultdict(list)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for item in edges:
        by_from[item.get("from")].append(item)
        by_to[item.get("to")].append(item)
        by_type[item.get("type")].append(item)
    return by_from, by_to, by_type


def nodes_with_label(nodes: dict[str, dict], label: str) -> list[dict]:
    return [item for item in nodes.values() if label in item.get("labels", [])]


def bundle_for_metric(
    metric: dict,
    nodes: dict[str, dict],
    by_from: dict[str, list[dict]],
    by_to: dict[str, list[dict]],
    *,
    max_sql_chars: int,
    max_sql_statements: int,
    max_columns: int,
    max_lineage: int,
) -> dict:
    metric_id = metric["id"]
    metric_props = dict(metric.get("properties", {}))
    metric_props["english_name_clean"] = strip_html(metric_props.get("english_name", ""))

    definitions = []
    datasets = []
    tasks = []
    sql_ids = set()
    evidence_node_ids = {metric_id}
    evidence_edge_ids = []

    for rel in by_from.get(metric_id, []):
        rel_type = rel.get("type")
        target = nodes.get(rel.get("to"))
        if not target:
            continue
        evidence_edge_ids.append(rel["id"])
        evidence_node_ids.add(target["id"])
        if rel_type == "HAS_DEFINITION":
            definitions.append(compact_definition(target))
        elif rel_type == "STORED_IN":
            datasets.append(compact_dataset(target))
        elif rel_type == "COMPUTED_BY":
            tasks.append(compact_task(target))

    for task in tasks:
        for rel in by_from.get(task["id"], []):
            if rel.get("type") == "EMITS_SQL":
                sql_ids.add(rel.get("to"))
                evidence_edge_ids.append(rel["id"])

    for dataset in datasets:
        for rel in by_to.get(dataset["id"], []):
            if rel.get("type") == "WRITES":
                sql_ids.add(rel.get("from"))
                evidence_edge_ids.append(rel["id"])

    sql_items = []
    read_tables = {}
    write_tables = {}
    for sql_id in sorted(sql_ids)[:max_sql_statements]:
        sql_node = nodes.get(sql_id)
        if not sql_node:
            continue
        evidence_node_ids.add(sql_id)
        sql_items.append(sql_text(sql_node, max_sql_chars))
        for rel in by_from.get(sql_id, []):
            target = nodes.get(rel.get("to"))
            if not target:
                continue
            if rel.get("type") == "READS":
                read_tables[target["id"]] = compact_dataset(target)
                evidence_edge_ids.append(rel["id"])
                evidence_node_ids.add(target["id"])
            elif rel.get("type") == "WRITES":
                write_tables[target["id"]] = compact_dataset(target)
                evidence_edge_ids.append(rel["id"])
                evidence_node_ids.add(target["id"])

    target_column_ids = set()
    columns = []
    for dataset in datasets:
        for rel in by_from.get(dataset["id"], []):
            if rel.get("type") != "HAS_COLUMN":
                continue
            col = nodes.get(rel.get("to"))
            if not col:
                continue
            target_column_ids.add(col["id"])
            evidence_node_ids.add(col["id"])
            evidence_edge_ids.append(rel["id"])
            if len(columns) < max_columns:
                columns.append(compact_column(col))

    column_lineage = []
    for col_id in sorted(target_column_ids):
        for rel in by_from.get(col_id, []):
            if rel.get("type") != "DERIVED_FROM":
                continue
            src = nodes.get(rel.get("to"))
            if not src:
                continue
            column_lineage.append(
                {
                    "target_column_id": col_id,
                    "source_column_id": src["id"],
                    "source_dataset": src.get("properties", {}).get("dataset"),
                    "source_column": src.get("properties", {}).get("name"),
                    "confidence": rel.get("properties", {}).get("confidence"),
                    "source_resolution": rel.get("properties", {}).get("source_resolution"),
                }
            )
            evidence_edge_ids.append(rel["id"])
            evidence_node_ids.add(src["id"])
            if len(column_lineage) >= max_lineage:
                break
        if len(column_lineage) >= max_lineage:
            break

    payload = {
        "metric": metric_props,
        "registered_definitions": definitions,
        "stored_in": datasets,
        "computed_by": tasks,
        "sql_statements": sql_items,
        "read_tables": list(read_tables.values()),
        "write_tables": list(write_tables.values()),
        "target_columns": columns,
        "column_lineage": column_lineage,
        "evidence_node_ids": sorted(evidence_node_ids),
        "evidence_edge_ids": sorted(set(evidence_edge_ids)),
    }
    bundle_id = "evidence:" + stable_hash(payload)[:24]
    return {
        "bundle_id": bundle_id,
        "metric_node_id": metric_id,
        "metric_id": metric_props.get("metric_id"),
        "input_hash": stable_hash(payload),
        "payload": payload,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--metric-id")
    parser.add_argument("--max-sql-chars", type=int, default=12000)
    parser.add_argument("--max-sql-statements", type=int, default=8)
    parser.add_argument("--max-columns", type=int, default=80)
    parser.add_argument("--max-lineage", type=int, default=120)
    args = parser.parse_args()

    base = Path(args.project_dir)
    nodes_list = load_jsonl(base / f"{args.prefix}_graph_nodes.jsonl")
    edges = load_jsonl(base / f"{args.prefix}_graph_edges.jsonl")
    nodes = {item["id"]: item for item in nodes_list}
    by_from, by_to, _ = edge_index(edges)

    metrics = nodes_with_label(nodes, "Metric")
    if args.metric_id:
        metrics = [
            item
            for item in metrics
            if item["id"] == args.metric_id or item.get("properties", {}).get("metric_id") == args.metric_id
        ]
    metrics.sort(key=lambda item: item["id"])
    if args.limit:
        metrics = metrics[: args.limit]

    bundles = [
        bundle_for_metric(
            metric,
            nodes,
            by_from,
            by_to,
            max_sql_chars=args.max_sql_chars,
            max_sql_statements=args.max_sql_statements,
            max_columns=args.max_columns,
            max_lineage=args.max_lineage,
        )
        for metric in metrics
    ]
    out_dir = base / "llm"
    write_jsonl(out_dir / "evidence_bundles.jsonl", bundles)
    summary = {
        "project_dir": str(base),
        "prefix": args.prefix,
        "bundle_count": len(bundles),
        "metrics_with_sql": sum(1 for item in bundles if item["payload"]["sql_statements"]),
        "metrics_with_registry_definition": sum(1 for item in bundles if item["payload"]["registered_definitions"]),
        "output_path": str(out_dir / "evidence_bundles.jsonl"),
    }
    (out_dir / "evidence_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
