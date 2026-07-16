#!/usr/bin/env python3
"""Audit graph facts for completeness, provenance, confidence, and connectivity."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


REQUIRED_NODE_PROPS = {
    "Dataset": ["name", "layer", "fact_type", "confidence", "build_id"],
    "Column": ["name", "dataset", "fact_type", "confidence", "build_id"],
    "ScheduleTask": ["task_id", "task_type", "fact_type", "confidence", "build_id"],
    "SqlStatement": ["statement_id", "task_id", "statement_path", "fact_type", "confidence", "build_id"],
    "Metric": ["metric_id", "dataset", "fact_type", "confidence", "build_id"],
}

REQUIRED_EDGE_PROPS = {
    "READS": ["task_id", "fact_type", "confidence", "build_id"],
    "WRITES": ["task_id", "fact_type", "confidence", "build_id"],
    "DATASET_DEPENDS_ON": ["fact_type", "confidence", "build_id"],
    "DERIVED_FROM": ["statement_id", "task_id", "source_resolution", "target_resolution", "fact_type", "confidence", "build_id"],
    "DEPENDS_ON": ["source_system", "fact_type", "confidence", "build_id"],
}


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def labels_of(node: dict) -> set[str]:
    return set(node.get("labels", []))


def audit(project_dir: Path, prefix: str) -> dict:
    nodes = load_jsonl(project_dir / f"{prefix}_graph_nodes.jsonl")
    edges = load_jsonl(project_dir / f"{prefix}_graph_edges.jsonl")
    node_by_id = {item["id"]: item for item in nodes}
    out_degree = Counter(edge["from"] for edge in edges)
    in_degree = Counter(edge["to"] for edge in edges)

    missing_node_props = []
    for node in nodes:
        props = node.get("properties", {})
        for label, required in REQUIRED_NODE_PROPS.items():
            if label not in labels_of(node):
                continue
            missing = [key for key in required if props.get(key) in (None, "", [], {})]
            if missing:
                missing_node_props.append({"id": node["id"], "label": label, "missing": missing})

    missing_edge_props = []
    for edge in edges:
        props = edge.get("properties", {})
        required = REQUIRED_EDGE_PROPS.get(edge["type"], [])
        missing = [key for key in required if props.get(key) in (None, "", [], {})]
        if missing:
            missing_edge_props.append({"id": edge["id"], "type": edge["type"], "missing": missing})

    missing_endpoints = [
        {"id": edge["id"], "from": edge["from"], "to": edge["to"], "type": edge["type"]}
        for edge in edges
        if edge["from"] not in node_by_id or edge["to"] not in node_by_id
    ]

    isolated = [
        node["id"]
        for node in nodes
        if out_degree[node["id"]] == 0
        and in_degree[node["id"]] == 0
        and "Project" not in labels_of(node)
        and "DataLayer" not in labels_of(node)
    ]

    edge_confidence = Counter(edge.get("properties", {}).get("confidence", "unknown") for edge in edges)
    node_confidence = Counter(node.get("properties", {}).get("confidence", "unknown") for node in nodes)
    edge_fact_type = Counter(edge.get("properties", {}).get("fact_type", "unknown") for edge in edges)
    node_fact_type = Counter(node.get("properties", {}).get("fact_type", "unknown") for node in nodes)

    dataset_ids = [node["id"] for node in nodes if "Dataset" in labels_of(node)]
    datasets_with_columns = {edge["from"] for edge in edges if edge["type"] == "HAS_COLUMN"}
    datasets_without_columns = [
        node_id for node_id in dataset_ids if node_id not in datasets_with_columns
    ]
    metrics = [node["id"] for node in nodes if "Metric" in labels_of(node)]
    metric_storage = defaultdict(int)
    metric_compute = defaultdict(int)
    for edge in edges:
        if edge["type"] == "STORED_IN":
            metric_storage[edge["from"]] += 1
        elif edge["type"] == "COMPUTED_BY":
            metric_compute[edge["from"]] += 1

    derived_edges = [edge for edge in edges if edge["type"] == "DERIVED_FROM"]
    generated_edges = [edge for edge in edges if edge["type"] == "GENERATED_BY_EXPRESSION"]
    unresolved_derived = [
        edge["id"]
        for edge in derived_edges
        if edge.get("properties", {}).get("confidence") == "low"
    ]

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "missing_endpoint_count": len(missing_endpoints),
        "missing_endpoint_sample": missing_endpoints[:20],
        "missing_node_required_prop_count": len(missing_node_props),
        "missing_node_required_prop_sample": missing_node_props[:20],
        "missing_edge_required_prop_count": len(missing_edge_props),
        "missing_edge_required_prop_sample": missing_edge_props[:20],
        "isolated_node_count": len(isolated),
        "isolated_node_sample": isolated[:20],
        "node_confidence_distribution": dict(node_confidence),
        "edge_confidence_distribution": dict(edge_confidence),
        "node_fact_type_distribution": dict(node_fact_type),
        "edge_fact_type_distribution": dict(edge_fact_type),
        "dataset_count": len(dataset_ids),
        "datasets_without_columns_count": len(datasets_without_columns),
        "datasets_without_columns_sample": datasets_without_columns[:20],
        "metric_count": len(metrics),
        "metrics_without_storage_count": sum(1 for metric_id in metrics if metric_storage[metric_id] == 0),
        "metrics_without_compute_task_count": sum(1 for metric_id in metrics if metric_compute[metric_id] == 0),
        "derived_from_count": len(derived_edges),
        "generated_by_expression_count": len(generated_edges),
        "low_confidence_derived_from_count": len(unresolved_derived),
        "low_confidence_derived_from_sample": unresolved_derived[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    result = audit(project_dir, args.prefix)
    out = project_dir / f"{args.prefix}_fact_audit.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"audit_path": str(out), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
