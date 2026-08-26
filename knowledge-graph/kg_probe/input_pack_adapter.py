"""Build the KG project's static artifacts from a SQL static-lineage Input Pack.

This is intentionally a thin, offline adapter. It does not invent scheduler
edges, runtime results, owners, or business acceptance facts.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from .extract_sql_facts import extract_project
except ImportError:  # pragma: no cover - supports direct script execution
    from extract_sql_facts import extract_project


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def layer_of(dataset: str) -> str:
    value = dataset.lower()
    if value.startswith("odata"):
        return "odata"
    if value.startswith("pdata"):
        return "pdata"
    if value.startswith("dm_index_n."):
        return "dm_index_n"
    if value.startswith("dm_") or value.startswith("dm."):
        return "dm"
    return "other"


def task_pack_paths(input_root: Path, task_ids: set[str] | None = None) -> list[Path]:
    paths = sorted((input_root / "tasks").glob("*/*/task.json"))
    selected = []
    seen: dict[str, Path] = {}
    for path in paths:
        task = load_json(path, {})
        task_id = str(task.get("taskId") or path.parent.name)
        if task_ids and task_id not in task_ids:
            continue
        if task_id in seen:
            raise ValueError(f"duplicate Task Pack for task {task_id}: {seen[task_id]} and {path}")
        seen[task_id] = path
        selected.append(path)
    return selected


def endpoint_name(endpoint: Any) -> str:
    if isinstance(endpoint, dict):
        return str(endpoint.get("qualifiedName") or "").strip().lower()
    if isinstance(endpoint, str):
        return endpoint.strip().lower()
    return ""


def split_ddl_columns(ddl: str) -> list[str]:
    """Extract simple column names from a CREATE TABLE column list."""
    start = ddl.find("(")
    if start < 0:
        return []
    depth = 0
    end = -1
    for index in range(start, len(ddl)):
        char = ddl[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        return []

    chunks: list[str] = []
    chunk_start = 0
    depth = 0
    for index, char in enumerate(ddl[start + 1 : end], start=start + 1):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            chunks.append(ddl[chunk_start:index])
            chunk_start = index + 1
    chunks.append(ddl[chunk_start:end])

    columns = []
    for chunk in chunks:
        match = re.match(r"\s*`?([A-Za-z_][\w]*)`?\s+", chunk)
        if match and match.group(1).lower() not in {"constraint", "primary", "unique", "key"}:
            columns.append(match.group(1))
    return columns


def build_table_metadata(
    input_root: Path,
    referenced_datasets: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    records = []
    warnings = []
    datasets_seen: dict[str, str] = {}
    for table_json_path in sorted((input_root / "tables").glob("*/**/table.json")):
        table = load_json(table_json_path, {})
        dataset = str(table.get("qualifiedName") or "").strip().lower()
        if not dataset:
            warnings.append(f"table pack missing qualifiedName: {table_json_path}")
            continue
        if referenced_datasets is not None and dataset not in referenced_datasets:
            continue
        data_source = str(table.get("dataSource") or "").strip()
        if dataset in datasets_seen and datasets_seen[dataset] != data_source:
            warnings.append(
                f"dataset identity collision retained by KG model: {dataset} "
                f"({datasets_seen[dataset]} vs {data_source})"
            )
        datasets_seen[dataset] = data_source
        ddl_path = table_json_path.parent / str((table.get("ddlFile") or {}).get("path") or "ddl.sql")
        ddl = ddl_path.read_text(encoding="utf-8", errors="replace") if ddl_path.exists() else ""
        record = {
            "qualifiedName": dataset,
            "dbName": table.get("schema") or dataset.split(".", 1)[0],
            "name": table.get("name") or dataset.rsplit(".", 1)[-1],
            "guid": table.get("guid", ""),
            "description": table.get("description") or "",
            "dataSource": data_source,
            "platform": table.get("platform", ""),
            "refColumns": [{"name": column} for column in split_ddl_columns(ddl)],
            "input_pack_table_path": str(table_json_path),
            "input_pack_ddl_path": str(ddl_path) if ddl_path.exists() else "",
        }
        records.append(
            {
                "dataset": dataset,
                "total": 1,
                "record_count": 1,
                "exact_count": 1,
                "records": [record],
                "exact_records": [record],
            }
        )
    return records, warnings


def add_input_pack_targets(
    task_docs: list[tuple[Path, dict]],
    statements: list[dict],
    datasets: list[dict],
    edges: list[dict],
) -> None:
    """Bind confirmed Task Pack targets without claiming SQL proved the name."""
    dataset_names = {item["dataset"] for item in datasets}
    for task_path, task in task_docs:
        task_id = str(task.get("taskId") or task_path.parent.name)
        target = endpoint_name(task.get("target"))
        if not target:
            continue
        if target not in dataset_names:
            datasets.append({"dataset": target, "layer": layer_of(target)})
            dataset_names.add(target)
        task_edges = [item for item in edges if item.get("task_id") == task_id]
        if any(item.get("relation") == "WRITES" for item in task_edges):
            continue
        candidates = [
            item
            for item in statements
            if item.get("task_id") == task_id
            and str(item.get("source_sql_path", "")).lower().replace("\\", "/").endswith(
                ("/query.sql", "/prepare.sql", "/finish.sql")
            )
        ]
        if not candidates:
            continue
        statement = candidates[-1]
        edges.append(
            {
                "from": statement["statement_id"],
                "to": target,
                "relation": "WRITES",
                "task_id": task_id,
                "source_type": "input_pack_task_target",
                "target_evidence_kind": task.get("targetEvidenceKind") or "UNKNOWN",
            }
        )


def build_project(
    input_root: Path,
    output_dir: Path,
    *,
    task_ids: list[str] | None = None,
    dialect: str = "hive",
    project_id: str | None = None,
) -> dict[str, Any]:
    input_root = input_root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_ids = set(task_ids or [])
    task_paths = task_pack_paths(input_root, selected_ids or None)
    if not task_paths:
        raise ValueError(f"no Task Packs found under {input_root}")

    project_key = project_id or output_dir.name
    task_docs = [(path, load_json(path, {})) for path in task_paths]
    found_task_ids = {str(task.get("taskId") or path.parent.name) for path, task in task_docs}
    missing_task_ids = sorted(selected_ids - found_task_ids)
    nodes = []
    details = []
    artifacts = []
    warnings: list[str] = []
    for task_path, task in task_docs:
        task_id = str(task.get("taskId") or task_path.parent.name)
        target_dataset = endpoint_name(task.get("target"))
        nodes.append(
            {
                "task_id": task_id,
                "task_name": task.get("taskName") or "",
                "topic_name": task.get("topicName") or "",
                "layer": layer_of(target_dataset) if target_dataset else "other",
                "depth": 0,
                "is_root": False,
                "input_pack_task_path": str(task_path),
            }
        )
        code_evidence = task.get("codeEvidence") or {}
        details.append(
            {
                "task_id": task_id,
                "task_type": task.get("taskCategory") or task.get("taskType") or "",
                "description": task.get("taskName") or "",
                "topic": task.get("topicName") or "",
                "cycle": task.get("scheduleCycle") or "",
                "source": endpoint_name(task.get("source")),
                "target": target_dataset,
                "script_path": code_evidence.get("codePath") or "",
                "owners": "",
                "sync_info": {},
                "source_system": "input_pack",
            }
        )
        for sql_file in task.get("sqlFiles") or []:
            relative_path = str(sql_file.get("path") or "")
            sql_path = task_path.parent / relative_path
            if not sql_path.exists():
                warnings.append(f"missing SQL slot for task {task_id}: {sql_path}")
                continue
            artifacts.append(
                {
                    "task_id": task_id,
                    "artifact_id": f"{task_id}:{sql_file.get('slot', sql_path.stem)}",
                    "path": str(sql_path),
                    "source_type": "task_page_input_pack",
                    "sql_slot": sql_file.get("slot") or sql_path.stem,
                    "sha256": sql_file.get("sha256") or "",
                }
            )

    lineage = {
        "project_id": project_key,
        "root_task_ids": [],
        "nodes": nodes,
        "edges": [],
        "terminal_task_ids": [],
        "errors": [],
        "summary": {
            "node_count": len(nodes),
            "edge_count": 0,
            "scheduler_edges_available": False,
            "evidence_scope": "INPUT_PACK_STATIC_ONLY",
        },
    }
    write_json(output_dir / "lineage.json", lineage)
    write_json(output_dir / "task_details.json", details)
    write_json(output_dir / "code_artifacts_page.json", artifacts)

    parsed = extract_project(output_dir, dialect, "code_artifacts_page.json")
    statements = []
    for item in parsed["statements"]:
        statement = dict(item)
        source_path = statement.pop("source_log_path", "")
        statement["source_sql_path"] = source_path
        statement["source_system"] = "input_pack"
        statement["source_type"] = "canonical_task_sql"
        statements.append(statement)
    datasets = parsed["datasets"]
    edges = [
        {**item, "source_type": "canonical_task_sql"}
        for item in parsed["edges"]
    ]
    add_input_pack_targets(task_docs, statements, datasets, edges)
    write_json(output_dir / "strategy_sql_statements.json", statements)
    write_json(output_dir / "strategy_datasets.json", datasets)
    write_json(output_dir / "strategy_dataset_edges.json", edges)
    write_json(output_dir / "strategy_sql_parse_errors.json", parsed["errors"])

    referenced_datasets = {item["dataset"] for item in datasets}
    dms_records, table_warnings = build_table_metadata(input_root, referenced_datasets)
    warnings.extend(table_warnings)
    write_json(output_dir / "sz_metadata" / "dataset_dms.json", dms_records)
    write_json(output_dir / "sz_metadata" / "indicator_registry.json", [])

    summary = {
        "project_id": project_key,
        "requested_task_count": len(selected_ids) if selected_ids else len(found_task_ids),
        "task_count": len(nodes),
        "missing_task_ids": missing_task_ids,
        "sql_artifact_count": len(artifacts),
        "statement_count": len(statements),
        "dataset_count": len(datasets),
        "dataset_edge_count": len(edges),
        "table_metadata_count": len(dms_records),
        "parse_error_count": len(parsed["errors"]),
        "scheduler_edges_available": False,
        "warnings": warnings,
    }
    write_json(output_dir / "input_pack_adapter_summary.json", summary)
    return summary


def run_graph_builder(output_dir: Path, prefix: str = "strategy") -> None:
    builder = Path(__file__).with_name("build_graph_facts.py")
    subprocess.run(
        [sys.executable, str(builder), str(output_dir), "--prefix", prefix],
        check=True,
    )


def parse_task_ids(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Consume a static-lineage Input Pack as a KG project.")
    parser.add_argument("input_pack_root")
    parser.add_argument("output_dir")
    parser.add_argument("--task-ids", default=None)
    parser.add_argument("--dialect", default="hive")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--build-graph", action="store_true")
    args = parser.parse_args()

    summary = build_project(
        Path(args.input_pack_root),
        Path(args.output_dir),
        task_ids=parse_task_ids(args.task_ids),
        dialect=args.dialect,
        project_id=args.project_id,
    )
    if args.build_graph:
        run_graph_builder(Path(args.output_dir).resolve())
        summary["graph_built"] = True
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
