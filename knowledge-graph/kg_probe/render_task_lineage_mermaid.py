"""Render one root task's complete table-level lineage as Mermaid."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import defaultdict, deque
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def task_id_from_node(node_id: str) -> str | None:
    if node_id.startswith("task:"):
        return node_id.removeprefix("task:")
    return None


def short_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def label(value: str) -> str:
    return html.escape(value.replace("\n", " "), quote=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--root-task", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    lineage = json.loads((args.project_dir / "lineage.json").read_text(encoding="utf-8"))
    relation_edges = [
        edge for edge in lineage["edges"] if edge.get("relation") == "UPSTREAM_OF"
    ]
    upstream_by_downstream: dict[str, list[str]] = defaultdict(list)
    for edge in relation_edges:
        upstream_by_downstream[edge["to_task_id"]].append(edge["from_task_id"])

    closure = {args.root_task}
    queue: deque[str] = deque([args.root_task])
    while queue:
        downstream = queue.popleft()
        for upstream in upstream_by_downstream.get(downstream, []):
            if upstream not in closure:
                closure.add(upstream)
                queue.append(upstream)

    nodes = load_jsonl(args.project_dir / "strategy_graph_nodes.jsonl")
    task_props: dict[str, dict] = {}
    for node in nodes:
        if "ScheduleTask" in node.get("labels", []):
            props = node.get("properties", {})
            task_id = str(props.get("task_id", ""))
            if task_id in closure:
                task_props[task_id] = props

    graph_edges = load_jsonl(args.project_dir / "strategy_graph_edges.jsonl")
    selected_edges: set[tuple[str, str, str]] = set()
    tables: set[str] = set()

    for edge in graph_edges:
        edge_type = edge.get("type")
        source = edge.get("from", "")
        target = edge.get("to", "")
        source_task = task_id_from_node(source)
        target_task = task_id_from_node(target)
        props = edge.get("properties", {})
        task_id = str(props.get("task_id", ""))

        if edge_type == "DEPENDS_ON" and source_task in closure and target_task in closure:
            selected_edges.add((f"task:{target_task}", f"task:{source_task}", "depends"))
        elif edge_type == "PRODUCES" and source_task in closure and target.startswith("dataset:"):
            tables.add(target.removeprefix("dataset:"))
            selected_edges.add((f"task:{source_task}", target, "produces"))
        elif edge_type == "READS" and task_id in closure and target.startswith("dataset:"):
            tables.add(target.removeprefix("dataset:"))
            selected_edges.add((target, f"task:{task_id}", "reads"))

    lines = ["flowchart TD"]
    lines.append("    %% Root-specific table/task closure; generated from KG lineage.json")
    for task_id, props in sorted(task_props.items(), key=lambda item: int(item[0])):
        task_node = f"task:{task_id}"
        mermaid_id = short_id("t", task_node)
        task_name = str(props.get("task_name", ""))
        task_type = str(props.get("task_type", ""))
        lines.append(
            f'    {mermaid_id}["{label(task_id + ": " + task_name)}<br/><small>{label(task_type)}</small>"]'
        )

    for table in sorted(tables):
        node_id = f"dataset:{table}"
        mermaid_id = short_id("d", node_id)
        lines.append(f'    {mermaid_id}[["{label(table)}"]]')

    for source, target, edge_type in sorted(selected_edges):
        source_id = short_id("t" if source.startswith("task:") else "d", source)
        target_id = short_id("t" if target.startswith("task:") else "d", target)
        arrow = "-.->" if edge_type == "depends" else "-->"
        lines.append(f"    {source_id} {arrow} {target_id}")

    lines.extend(
        [
            f"    classDef target fill:#ffd166,stroke:#8a5a00,stroke-width:3px;",
            f"    classDef task fill:#dbeafe,stroke:#2563eb;",
            f"    classDef checker fill:#f3e8ff,stroke:#9333ea;",
            f"    classDef table fill:#dcfce7,stroke:#16a34a;",
            f"    classDef source fill:#f3f4f6,stroke:#6b7280;",
        ]
    )

    for task_id, props in task_props.items():
        node_id = short_id("t", f"task:{task_id}")
        task_type = str(props.get("task_type", ""))
        class_name = "target" if task_id == args.root_task else (
            "checker" if task_type == "checkdbflag" else "task"
        )
        lines.append(f"    class {node_id} {class_name}")
    for table in tables:
        node_id = short_id("d", f"dataset:{table}")
        lines.append(f"    class {node_id} table")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "root_task": args.root_task,
                "task_count": len(task_props),
                "table_count": len(tables),
                "edge_count": len(selected_edges),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
