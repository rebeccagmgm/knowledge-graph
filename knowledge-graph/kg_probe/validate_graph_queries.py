#!/usr/bin/env python3
"""Run offline graph query validations against graph JSONL facts."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    items = []
    if not path.exists():
        return items
    for line in path.read_text().splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items


def first_label(node: dict) -> str:
    labels = node.get("labels", [])
    return labels[0] if labels else ""


def dataset_layer(node: dict) -> str:
    return node.get("properties", {}).get("layer", "")


def compact_path(path: list[str], nodes: dict[str, dict]) -> list[dict]:
    result = []
    for node_id in path:
        node = nodes.get(node_id, {})
        props = node.get("properties", {})
        result.append(
            {
                "id": node_id,
                "label": first_label(node),
                "name": props.get("name") or props.get("task_name") or props.get("chinese_name") or node_id,
                "layer": props.get("layer", ""),
            }
        )
    return result


def bfs_paths(
    start: str,
    adjacency: dict[str, list[str]],
    nodes: dict[str, dict],
    target_fn,
    max_depth: int,
    limit: int,
) -> list[list[dict]]:
    paths = []
    queue = deque([(start, [start])])
    seen = {(start, 0)}
    while queue and len(paths) < limit:
        node_id, path = queue.popleft()
        depth = len(path) - 1
        if depth > 0 and target_fn(node_id, nodes.get(node_id, {})):
            paths.append(compact_path(path, nodes))
            continue
        if depth >= max_depth:
            continue
        for nxt in adjacency.get(node_id, []):
            state = (nxt, depth + 1)
            if state in seen:
                continue
            seen.add(state)
            queue.append((nxt, [*path, nxt]))
    return paths


def build_indexes(nodes_list: list[dict], edges_list: list[dict]) -> dict:
    nodes = {item["id"]: item for item in nodes_list}
    out_edges: dict[str, list[dict]] = defaultdict(list)
    in_edges: dict[str, list[dict]] = defaultdict(list)
    for edge in edges_list:
        out_edges[edge["from"]].append(edge)
        in_edges[edge["to"]].append(edge)

    task_upstream: dict[str, list[str]] = defaultdict(list)
    dataset_upstream: dict[str, set[str]] = defaultdict(set)
    dataset_written_by_sql: dict[str, set[str]] = defaultdict(set)
    sql_reads: dict[str, set[str]] = defaultdict(set)
    dataset_producers: dict[str, set[str]] = defaultdict(set)
    task_dataset_outputs: dict[str, set[str]] = defaultdict(set)

    for edge in edges_list:
        if edge["type"] == "DEPENDS_ON":
            task_upstream[edge["from"]].append(edge["to"])
        elif edge["type"] == "READS":
            sql_reads[edge["from"]].add(edge["to"])
        elif edge["type"] == "WRITES":
            dataset_written_by_sql[edge["to"]].add(edge["from"])
        elif edge["type"] == "PRODUCES":
            dataset_producers[edge["to"]].add(edge["from"])
            task_dataset_outputs[edge["from"]].add(edge["to"])

    for target_dataset, writers in dataset_written_by_sql.items():
        for sql_id in writers:
            dataset_upstream[target_dataset].update(sql_reads.get(sql_id, set()))

    for target_dataset, producers in dataset_producers.items():
        for task_id in producers:
            for upstream_task in task_upstream.get(task_id, []):
                dataset_upstream[target_dataset].update(task_dataset_outputs.get(upstream_task, set()))

    return {
        "nodes": nodes,
        "out_edges": out_edges,
        "in_edges": in_edges,
        "task_upstream": task_upstream,
        "dataset_upstream": {key: sorted(value) for key, value in dataset_upstream.items()},
    }


def graph_summary(nodes: dict[str, dict], edges: list[dict]) -> dict:
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_label_distribution": dict(Counter(first_label(item) for item in nodes.values())),
        "edge_type_distribution": dict(Counter(item["type"] for item in edges)),
    }


def validate(project_dir: Path, prefix: str, sample_limit: int, path_limit: int, max_depth: int) -> dict:
    nodes_list = load_jsonl(project_dir / f"{prefix}_graph_nodes.jsonl")
    edges_list = load_jsonl(project_dir / f"{prefix}_graph_edges.jsonl")
    idx = build_indexes(nodes_list, edges_list)
    nodes = idx["nodes"]

    missing_endpoints = [
        edge["id"]
        for edge in edges_list
        if edge.get("from") not in nodes or edge.get("to") not in nodes
    ]

    dataset_adj = idx["dataset_upstream"]
    dataset_nodes = [
        node_id
        for node_id, item in nodes.items()
        if "Dataset" in item.get("labels", []) and dataset_layer(item) in {"dm", "dm_index_n"}
    ]
    dataset_nodes = sorted(
        dataset_nodes,
        key=lambda node_id: len(dataset_adj.get(node_id, [])),
        reverse=True,
    )[:sample_limit]

    dataset_traces = {}
    for dataset_id in dataset_nodes:
        dataset_traces[dataset_id] = bfs_paths(
            dataset_id,
            dataset_adj,
            nodes,
            lambda _node_id, node: "Dataset" in node.get("labels", []) and dataset_layer(node) in {"odata", "pdata"},
            max_depth=max_depth,
            limit=path_limit,
        )

    root_tasks = [
        edge["to"]
        for edge in edges_list
        if edge.get("type") == "HAS_ENTRY_TASK" and edge.get("to") in nodes
    ][:sample_limit]
    task_traces = {}
    for task_id in root_tasks:
        task_traces[task_id] = bfs_paths(
            task_id,
            idx["task_upstream"],
            nodes,
            lambda node_id, _node: not idx["task_upstream"].get(node_id),
            max_depth=max_depth,
            limit=path_limit,
        )

    metrics = [
        node_id
        for node_id, item in nodes.items()
        if "Metric" in item.get("labels", [])
    ][:sample_limit]
    metric_checks = []
    out_edges = idx["out_edges"]
    for metric_id in metrics:
        stored = [edge["to"] for edge in out_edges.get(metric_id, []) if edge["type"] == "STORED_IN"]
        computed = [edge["to"] for edge in out_edges.get(metric_id, []) if edge["type"] == "COMPUTED_BY"]
        definitions = [edge["to"] for edge in out_edges.get(metric_id, []) if edge["type"] == "HAS_DEFINITION"]
        metric_checks.append(
            {
                "metric_id": metric_id,
                "stored_in_count": len(stored),
                "computed_by_count": len(computed),
                "definition_count": len(definitions),
            }
        )

    return {
        "summary": graph_summary(nodes, edges_list),
        "endpoint_check": {
            "missing_edge_endpoint_count": len(missing_endpoints),
            "sample": missing_endpoints[:5],
        },
        "dataset_trace_check": {
            "sample_dataset_count": len(dataset_nodes),
            "datasets_with_source_path": sum(bool(paths) for paths in dataset_traces.values()),
            "samples": dataset_traces,
        },
        "task_trace_check": {
            "sample_task_count": len(root_tasks),
            "tasks_with_terminal_path": sum(bool(paths) for paths in task_traces.values()),
            "samples": task_traces,
        },
        "metric_check": {
            "sample_metric_count": len(metrics),
            "metrics_with_storage": sum(item["stored_in_count"] > 0 for item in metric_checks),
            "metrics_with_compute_task": sum(item["computed_by_count"] > 0 for item in metric_checks),
            "metrics_with_definition": sum(item["definition_count"] > 0 for item in metric_checks),
            "samples": metric_checks,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument("--path-limit", type=int, default=3)
    parser.add_argument("--max-depth", type=int, default=12)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    report = validate(project_dir, args.prefix, args.sample_limit, args.path_limit, args.max_depth)
    out = project_dir / f"{args.prefix}_graph_query_validation.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    compact = {
        "output": str(out),
        "node_count": report["summary"]["node_count"],
        "edge_count": report["summary"]["edge_count"],
        "missing_edge_endpoint_count": report["endpoint_check"]["missing_edge_endpoint_count"],
        "datasets_with_source_path": report["dataset_trace_check"]["datasets_with_source_path"],
        "sample_dataset_count": report["dataset_trace_check"]["sample_dataset_count"],
        "tasks_with_terminal_path": report["task_trace_check"]["tasks_with_terminal_path"],
        "sample_task_count": report["task_trace_check"]["sample_task_count"],
        "metrics_with_storage": report["metric_check"]["metrics_with_storage"],
        "metrics_with_compute_task": report["metric_check"]["metrics_with_compute_task"],
        "sample_metric_count": report["metric_check"]["sample_metric_count"],
    }
    print(json.dumps(compact, ensure_ascii=False))


if __name__ == "__main__":
    main()
