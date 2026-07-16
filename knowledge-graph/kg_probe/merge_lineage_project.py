#!/usr/bin/env python3
"""Merge multiple root-task lineage snapshots into one project-level graph."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path


DEFAULT_TASKS = [
    "236334",
    "212769",
    "207174",
    "196612",
    "194941",
    "191790",
    "191057",
    "165356",
    "158798",
    "158267",
    "155414",
    "152927",
    "152755",
    "152285",
    "149840",
    "132958",
    "114325",
    "109923",
    "105185",
    "101404",
]


def parse_tasks(value: str | None, task_file: str | None) -> list[str]:
    if task_file:
        text = Path(task_file).read_text()
        return [item.strip() for item in text.replace(",", "\n").splitlines() if item.strip()]
    if value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return DEFAULT_TASKS


def load_lineage(path: Path) -> dict:
    return json.loads(path.read_text())


def infer_depths(root_ids: list[str], edges: list[dict]) -> dict[str, int]:
    upstream_by_downstream = defaultdict(list)
    for edge in edges:
        upstream_by_downstream[edge["to_task_id"]].append(edge["from_task_id"])

    depths: dict[str, int] = {}
    queue = deque((root, 0) for root in root_ids)
    while queue:
        task_id, depth = queue.popleft()
        old = depths.get(task_id)
        if old is not None and old <= depth:
            continue
        depths[task_id] = depth
        for upstream_id in upstream_by_downstream.get(task_id, []):
            queue.append((upstream_id, depth + 1))
    return depths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="trial_project")
    parser.add_argument("--tasks", default=None)
    parser.add_argument("--task-file", default=None)
    parser.add_argument("--lineage-root", default="/Applications/personal-work/kg-code-snapshots/lineage_batch")
    parser.add_argument("--output-root", default="/Applications/personal-work/kg-code-snapshots/projects")
    args = parser.parse_args()

    root_ids = parse_tasks(args.tasks, args.task_file)
    lineage_root = Path(args.lineage_root)
    project_dir = Path(args.output_root) / args.project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    nodes_by_id = {}
    edges_by_key = {}
    roots_found = []
    missing_roots = []
    source_files = []
    errors = []

    for root_id in root_ids:
        path = lineage_root / f"{root_id}_lineage.json"
        if not path.exists():
            missing_roots.append(root_id)
            continue
        data = load_lineage(path)
        roots_found.append(root_id)
        source_files.append(str(path))
        errors.extend({"root_task_id": root_id, **err} for err in data.get("errors", []))

        for node in data.get("nodes", []):
            task_id = node["task_id"]
            existing = nodes_by_id.get(task_id)
            if not existing:
                nodes_by_id[task_id] = dict(node)
                nodes_by_id[task_id]["root_task_ids"] = []
            if root_id not in nodes_by_id[task_id]["root_task_ids"]:
                nodes_by_id[task_id]["root_task_ids"].append(root_id)
            # Preserve better names/details from non-root appearances.
            if not nodes_by_id[task_id].get("task_name") and node.get("task_name"):
                nodes_by_id[task_id]["task_name"] = node["task_name"]
            if nodes_by_id[task_id].get("layer") == "root_unknown" and node.get("layer") != "root_unknown":
                nodes_by_id[task_id]["layer"] = node.get("layer", nodes_by_id[task_id]["layer"])

        for edge in data.get("edges", []):
            key = (edge["from_task_id"], edge["to_task_id"])
            if key not in edges_by_key:
                edges_by_key[key] = dict(edge)
                edges_by_key[key]["root_task_ids"] = []
            if root_id not in edges_by_key[key]["root_task_ids"]:
                edges_by_key[key]["root_task_ids"].append(root_id)

    depths = infer_depths(roots_found, list(edges_by_key.values()))
    for task_id, node in nodes_by_id.items():
        node["depth"] = depths.get(task_id, node.get("depth", 0))
        node["is_root"] = task_id in roots_found

    has_upstream = {edge["to_task_id"] for edge in edges_by_key.values()}
    pending_retry_ids = {err["task_id"] for err in errors if err.get("status") == "pending_retry"}
    terminal_ids = sorted(
        task_id
        for task_id in nodes_by_id
        if task_id not in has_upstream and task_id not in pending_retry_ids
    )

    nodes = sorted(nodes_by_id.values(), key=lambda item: (item.get("depth", 0), item["task_id"]))
    edges = sorted(edges_by_key.values(), key=lambda item: (item["to_task_id"], item["from_task_id"]))

    graph = {
        "project_id": args.project_id,
        "root_task_ids": roots_found,
        "missing_root_task_ids": missing_roots,
        "source_lineage_files": source_files,
        "nodes": nodes,
        "edges": edges,
        "terminal_task_ids": terminal_ids,
        "errors": errors,
        "summary": {
            "root_count": len(roots_found),
            "missing_root_count": len(missing_roots),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "max_observed_depth": max((node.get("depth", 0) for node in nodes), default=0),
            "frontier_by_depth": dict(Counter(node.get("depth", 0) for node in nodes)),
            "layer_distribution": dict(Counter(node.get("layer", "unknown") for node in nodes)),
            "terminal_count": len(terminal_ids),
            "terminal_layer_distribution": dict(
                Counter(nodes_by_id[task_id].get("layer", "unknown") for task_id in terminal_ids)
            ),
            "error_count": len(errors),
            "pending_retry_count": len(pending_retry_ids),
        },
    }

    output_path = project_dir / "lineage.json"
    output_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2))
    print(json.dumps({"project_dir": str(project_dir), "lineage_path": str(output_path), **graph["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
