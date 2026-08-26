#!/usr/bin/env python3
"""Run the KG pipeline from live OpenCLI Horae evidence.

This entry point keeps the KG fact builders and graph model unchanged.  It
replaces only the external collection boundary: live Horae relation/detail
calls are normalized into the same lineage, task-detail, and page-SQL
artifacts consumed by the existing KG builders.

It intentionally does not read Input Pack files, infer scheduler edges from
SQL, or claim runtime success from task metadata.  Runtime logs are collected
through the read-only schedule MCP when the selected task exposes a log body.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = SCRIPT_DIR.parent / "artifacts" / "opencli-projects"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


REUSABLE_FILES = (
    "lineage.json",
    "horae_relation_snapshots.json",
    "task_details.json",
    "task_detail_errors.json",
    "code_artifacts_page.json",
    "code_artifacts_page_errors.json",
    "task_instances_full.json",
    "log_artifacts_full.json",
    "log_collection_errors.json",
)


def rewrite_artifact_paths(value: Any, source_dir: Path, target_dir: Path) -> Any:
    if isinstance(value, list):
        return [rewrite_artifact_paths(item, source_dir, target_dir) for item in value]
    if isinstance(value, dict):
        return {key: rewrite_artifact_paths(item, source_dir, target_dir) for key, item in value.items()}
    if isinstance(value, str):
        try:
            relative = Path(value).resolve().relative_to(source_dir.resolve())
        except ValueError:
            return value
        return str(target_dir / relative)
    return value


def seed_reusable_project(source_dir: Path, target_dir: Path) -> None:
    """Copy only raw evidence from a previous OpenCLI project.

    Derived graph files are deliberately not copied.  The merged evidence is
    re-parsed and the graph is rebuilt once, so duplicate nodes and stale
    lineage facts cannot be introduced by concatenating graph artifacts.
    """
    if not (source_dir / "lineage.json").exists():
        raise SystemExit(f"Reuse project has no lineage.json: {source_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    for relative in (Path("logs"), Path("sql") / "page"):
        source = source_dir / relative
        if source.exists():
            shutil.copytree(source, target_dir / relative, dirs_exist_ok=True)
    for filename in REUSABLE_FILES:
        source = source_dir / filename
        if not source.exists():
            continue
        value = json.loads(source.read_text(encoding="utf-8"))
        if filename in {"code_artifacts_page.json", "log_artifacts_full.json"}:
            value = rewrite_artifact_paths(value, source_dir, target_dir)
        write_json(target_dir / filename, value)


def load_json_or(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def run_opencli(
    args: list[str],
    retries: int = 3,
    backoff_seconds: float = 0.8,
    timeout_seconds: int = 60,
) -> Any:
    executable = shutil.which("opencli.cmd") or shutil.which("opencli") or "opencli"
    command = [executable, *args, "-f", "json"]
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    last_error = ""
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                check=False,
                env=env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            last_error = f"OpenCLI timed out after {timeout_seconds}s"
            if attempt < retries:
                time.sleep(backoff_seconds * (2**attempt))
            continue
        if proc.returncode == 0:
            try:
                return json.loads(decode_output(proc.stdout))
            except json.JSONDecodeError as exc:
                preview = " ".join(decode_output(proc.stdout).split())[:500]
                last_error = f"OpenCLI returned non-JSON: {preview}"
        else:
            detail = (decode_output(proc.stderr) or decode_output(proc.stdout)).strip().replace("\n", " ")
            last_error = f"OpenCLI failed ({proc.returncode}): {detail[:500]}"
        if attempt < retries:
            time.sleep(backoff_seconds * (2**attempt))
    raise RuntimeError(f"{' '.join(command)}: {last_error}")


def layer_of(task_name: str) -> str:
    name = (task_name or "").lower()
    if name.startswith("odata") or ".odata" in name:
        return "odata"
    if name.startswith("pdata") or ".pdata" in name:
        return "pdata"
    if name.startswith("dm_index_n") or ".dm_index_n" in name:
        return "dm_index_n"
    if name.startswith("dm_") or name.startswith("dm.") or ".dm_" in name:
        return "dm"
    return "other"


def normalize_relation(raw: dict[str, Any]) -> dict[str, Any]:
    task_id = str(raw.get("task_id") or raw.get("id") or "").strip()
    task_name = str(raw.get("task_name") or raw.get("name") or "").strip()
    return {
        "task_id": task_id,
        "task_name": task_name,
        "topic_name": raw.get("topic_name") or raw.get("topic") or "",
        "in_charge": raw.get("in_charge") or raw.get("owner") or "",
        "layer": layer_of(task_name),
    }


def collect_lineage(
    roots: list[str],
    max_depth: int,
    max_nodes: int,
    sleep_seconds: float,
    retries: int,
    seed_lineage: dict[str, Any] | None = None,
    seed_snapshots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nodes = {
        str(item["task_id"]): dict(item)
        for item in (seed_lineage or {}).get("nodes", [])
        if item.get("task_id")
    }
    edges: list[dict[str, Any]] = list((seed_lineage or {}).get("edges", []))
    queue: deque[tuple[str, int]] = deque()
    visited: set[str] = set((seed_snapshots or {}).keys())
    relation_snapshots: dict[str, Any] = dict(seed_snapshots or {})

    for root in roots:
        if root not in nodes:
            nodes[root] = {
                "task_id": root,
                "task_name": "",
                "topic_name": "",
                "in_charge": "",
                "layer": "root_unknown",
                "depth": 0,
                "is_root": True,
            }
        else:
            nodes[root]["is_root"] = True
            nodes[root]["depth"] = 0
        queue.append((root, 0))

    errors: list[dict[str, Any]] = []
    while queue:
        task_id, depth = queue.popleft()
        if task_id in visited or depth >= max_depth:
            continue
        if len(nodes) >= max_nodes:
            errors.append({"task_id": task_id, "error": "max_nodes_reached"})
            break
        visited.add(task_id)
        try:
            raw_rows = run_opencli(
                ["horae", "relation", task_id, "--direction", "up", "--depth", "1"],
                retries=retries,
            )
            if not isinstance(raw_rows, list):
                raise RuntimeError(f"unexpected relation payload type: {type(raw_rows).__name__}")
            relation_snapshots[task_id] = raw_rows
            for raw in raw_rows:
                upstream = normalize_relation(raw)
                upstream_id = upstream["task_id"]
                if not upstream_id:
                    continue
                if upstream_id not in nodes:
                    upstream["depth"] = depth + 1
                    upstream["is_root"] = False
                    nodes[upstream_id] = upstream
                    queue.append((upstream_id, depth + 1))
                edge = {
                    "from_task_id": upstream_id,
                    "to_task_id": task_id,
                    "relation": "UPSTREAM_OF",
                    "depth_from_root": depth + 1,
                }
                if not any(
                    item["from_task_id"] == upstream_id and item["to_task_id"] == task_id
                    for item in edges
                ):
                    edges.append(edge)
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "error": str(exc), "status": "collection_failed"})
        if sleep_seconds:
            time.sleep(sleep_seconds)

    has_upstream = {edge["to_task_id"] for edge in edges}
    source_ids = sorted(task_id for task_id in nodes if task_id not in has_upstream)
    return {
        "project_id": "",
        "root_task_ids": roots,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "max_depth": max_depth,
        "nodes": sorted(nodes.values(), key=lambda item: (item.get("depth", 0), item["task_id"])),
        "edges": edges,
        "source_task_ids": source_ids,
        "errors": errors,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "expanded_count": len(visited),
            "max_observed_depth": max((n.get("depth", 0) for n in nodes.values()), default=0),
            "source_node_count": len(source_ids),
            "error_count": len(errors),
            "relation_snapshot_count": len(relation_snapshots),
        },
        "relation_snapshots": relation_snapshots,
    }


def normalize_detail(raw: dict[str, Any], task_id: str) -> dict[str, Any]:
    sync_info = raw.get("syncInfo") or {}
    return {
        "task_id": str(raw.get("id") or task_id),
        "description": raw.get("name", ""),
        "owners": raw.get("owner", ""),
        "source": raw.get("source", ""),
        "task_type": raw.get("taskType", ""),
        "topic": raw.get("topic", ""),
        "hive_db": raw.get("hiveDb", ""),
        "cycle": raw.get("cycle", ""),
        "cluster": raw.get("cluster", ""),
        "priority": raw.get("priority", ""),
        "retry_limit": raw.get("retryLimit", ""),
        "script_path": raw.get("scriptPath", ""),
        "file_name": raw.get("fileName", ""),
        "sync_info": sync_info,
        "target": "",
        "insert_mode": "",
        "dependency": raw.get("dependency"),
        "source_system": "horae",
        "source_type": "opencli_horae_detail",
    }


def collect_details_and_sql(
    project_dir: Path,
    task_ids: list[str],
    root_task_ids: set[str],
    sleep_seconds: float,
    retries: int,
) -> None:
    details: list[dict[str, Any]] = load_json_or(project_dir / "task_details.json", [])
    detail_errors: list[dict[str, Any]] = load_json_or(project_dir / "task_detail_errors.json", [])
    artifacts: list[dict[str, Any]] = load_json_or(project_dir / "code_artifacts_page.json", [])
    existing_task_ids = {str(item.get("task_id", "")) for item in details}
    sql_dir = project_dir / "sql" / "page"
    sql_dir.mkdir(parents=True, exist_ok=True)

    for task_id in task_ids:
        if task_id in existing_task_ids:
            continue
        try:
            horae_rows = run_opencli(["horae", "detail", task_id], retries=retries)
            if not isinstance(horae_rows, list) or not horae_rows:
                raise RuntimeError("empty Horae detail response")
            raw = horae_rows[0]

            detail = normalize_detail(raw, task_id)
            query_sql = str(raw.get("querySql") or "").strip()
            if task_id in root_task_ids:
                inspect_payload = run_opencli(
                    ["szdata", "task-inspect", "--task-ids", task_id, "--include", "detail,sql"],
                    retries=retries,
                )
                inspected_rows = inspect_payload[0].get("tasks", []) if isinstance(inspect_payload, list) and inspect_payload else []
                inspected = inspected_rows[0] if inspected_rows else {}
                target = str(inspected.get("targetTable") or "").strip()
                if target and target != "-":
                    detail["target"] = target
                detail["insert_mode"] = inspected.get("insertMode", "")
                sql_info = inspected.get("sql") or {}
                sql_parts = [
                    str(sql_info.get("createSql") or "").strip(),
                    str(sql_info.get("querySql") or query_sql).strip(),
                ]
                query_sql = "\n\n".join(part for part in sql_parts if part)
            details.append(detail)
            existing_task_ids.add(task_id)
            if query_sql:
                path = sql_dir / f"{task_id}_query.sql"
                path.write_text(query_sql, encoding="utf-8")
                artifacts.append(
                    {
                        "task_id": task_id,
                        "artifact_id": f"{task_id}_query",
                        "source_system": "horae",
                        "source_type": "task_page_opencli_horae_detail_sql",
                        "prop_name": "querySql",
                        "path": str(path),
                        "bytes": path.stat().st_size,
                    }
                )
            write_json(project_dir / "task_details.json", details)
            write_json(project_dir / "task_detail_errors.json", detail_errors)
            write_json(project_dir / "code_artifacts_page.json", artifacts)
            write_json(project_dir / "code_artifacts_page_errors.json", detail_errors)
        except Exception as exc:  # noqa: BLE001
            detail_errors.append({"task_id": task_id, "error": str(exc), "status": "collection_failed"})
            write_json(project_dir / "task_details.json", details)
            write_json(project_dir / "task_detail_errors.json", detail_errors)
            write_json(project_dir / "code_artifacts_page.json", artifacts)
            write_json(project_dir / "code_artifacts_page_errors.json", detail_errors)
        if sleep_seconds:
            time.sleep(sleep_seconds)

    write_json(project_dir / "task_details.json", details)
    write_json(project_dir / "task_detail_errors.json", detail_errors)
    write_json(project_dir / "code_artifacts_page.json", artifacts)
    write_json(project_dir / "code_artifacts_page_errors.json", detail_errors)


def collect_runtime_logs(
    project_dir: Path,
    details: list[dict[str, Any]],
    sleep_seconds: float,
    retries: int,
    max_log_chars: int,
) -> None:
    """Collect the selected latest log body through the OpenCLI schedule MCP."""
    logs_dir = project_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    instance_facts: list[dict[str, Any]] = load_json_or(project_dir / "task_instances_full.json", [])
    log_facts: list[dict[str, Any]] = load_json_or(project_dir / "log_artifacts_full.json", [])
    errors: list[dict[str, Any]] = load_json_or(project_dir / "log_collection_errors.json", [])
    existing_instance_ids = {str(item.get("task_id", "")) for item in instance_facts}
    wanted_types = {"hiveTask", "hiveTask-2.0"}

    for detail in details:
        task_id = str(detail.get("task_id", ""))
        if detail.get("task_type") not in wanted_types:
            continue
        if task_id in existing_instance_ids:
            continue
        try:
            rows = run_opencli(["horae", "instance", task_id, "--size", "1"], retries=retries)
            if not isinstance(rows, list) or not rows:
                instance_facts.append({"task_id": task_id, "selected": False})
                continue
            instance = rows[0]
            run_date = str(instance.get("run_date") or instance.get("data_time") or "")[:10]
            instance_facts.append(
                {
                    "task_id": task_id,
                    "selected": bool(run_date),
                    "run_date": instance.get("run_date", ""),
                    "data_time": instance.get("data_time", ""),
                    "state": instance.get("state", ""),
                    "is_success": str(instance.get("state", "")).upper() in {"SUCCESSFUL", "SUCCESS"},
                    "begin_time": instance.get("begin_time", ""),
                    "end_time": instance.get("end_time", ""),
                    "duration": instance.get("duration", ""),
                    "log_filename": instance.get("log_filename", ""),
                    "has_log_url": bool(instance.get("log_url")),
                }
            )
            existing_instance_ids.add(task_id)
            if not run_date:
                continue
            log_rows = run_opencli(
                [
                    "szdata",
                    "schedule-mcp-run-logs",
                    "--task-id",
                    task_id,
                    "--data-date",
                    run_date,
                    "--log-preview",
                    str(max_log_chars),
                ],
                retries=retries,
            )
            if not isinstance(log_rows, list) or not log_rows:
                errors.append({"task_id": task_id, "error": "empty runtime log response"})
                continue
            log_row = log_rows[0]
            content = str(log_row.get("fullLogPreview") or "")
            if not content:
                errors.append({"task_id": task_id, "error": "runtime log body unavailable"})
                continue
            expected_chars = int(log_row.get("fullLogChars") or 0)
            if expected_chars and len(content) < expected_chars:
                errors.append(
                    {
                        "task_id": task_id,
                        "error": "runtime log body truncated",
                        "expected_chars": expected_chars,
                        "collected_chars": len(content),
                    }
                )
            path = logs_dir / f"{task_id}_{run_date.replace('-', '')}.log"
            path.write_text(content, encoding="utf-8")
            low = content.lower()
            log_facts.append(
                {
                    "task_id": task_id,
                    "path": str(path),
                    "run_date": instance.get("run_date", ""),
                    "bytes": path.stat().st_size,
                    "line_count": content.count("\n") + 1,
                    "insert_count": low.count("insert "),
                    "select_count": low.count("select "),
                    "from_count": low.count(" from "),
                    "join_count": low.count(" join "),
                    "source_system": "opencli_schedule_mcp",
                    "source_type": "runtime_log_body",
                    "full_log_chars": expected_chars,
                    "collected_log_chars": len(content),
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "error": str(exc)})
        if sleep_seconds:
            time.sleep(sleep_seconds)

    write_json(project_dir / "task_instances_full.json", instance_facts)
    write_json(project_dir / "log_artifacts_full.json", log_facts)
    write_json(project_dir / "log_collection_errors.json", errors)


def run_step(name: str, command: list[str], project_dir: Path) -> dict[str, Any]:
    proc = subprocess.run(command, capture_output=True, check=False)
    stdout = decode_output(proc.stdout)
    stderr = decode_output(proc.stderr)
    result = {
        "step": name,
        "returncode": proc.returncode,
        "stdout_tail": [line for line in stdout.splitlines() if line.strip()][-5:],
        "stderr_tail": [line for line in stderr.splitlines() if line.strip()][-5:],
    }
    if proc.returncode != 0:
        write_json(project_dir / "opencli_pipeline_failure.json", result)
        raise SystemExit(f"Step failed: {name}")
    return result


def decode_output(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run KG from live OpenCLI Horae evidence.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--tasks", required=True, help="Comma-separated root Horae task IDs")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-nodes", type=int, default=3000)
    parser.add_argument("--sleep", type=float, default=0.08)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max-log-chars", type=int, default=5000000)
    parser.add_argument("--dialect", default="spark")
    parser.add_argument("--field-target", default=None, help="Optional target dataset for final field-path query")
    parser.add_argument("--fields", default=None, help="Comma-separated target fields")
    parser.add_argument("--field-output", default=None)
    parser.add_argument(
        "--reuse-project",
        default=None,
        help="Previous OpenCLI project directory whose raw evidence should be reused",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    roots = [item.strip() for item in args.tasks.replace("\n", ",").split(",") if item.strip()]
    if not roots:
        raise SystemExit("--tasks must contain at least one task ID")
    project_dir = Path(args.output_root) / args.project_id
    if args.dry_run:
        print(json.dumps({"project_dir": str(project_dir), "roots": roots}, ensure_ascii=False))
        return
    if args.reuse_project:
        source_dir = Path(args.reuse_project).resolve()
        if source_dir == project_dir.resolve():
            raise SystemExit("--reuse-project must be different from the output project")
        if project_dir.exists() and any(project_dir.iterdir()):
            if not (project_dir / "lineage.json").exists() or (project_dir / "opencli_pipeline_manifest.json").exists():
                raise SystemExit(f"Output project is not an empty or prepared reuse project: {project_dir}")
        else:
            seed_reusable_project(source_dir, project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    seed_lineage = load_json_or(project_dir / "lineage.json", None) if args.reuse_project else None
    seed_snapshots = load_json_or(project_dir / "horae_relation_snapshots.json", {}) if args.reuse_project else None
    lineage = collect_lineage(
        roots,
        args.max_depth,
        args.max_nodes,
        args.sleep,
        args.retries,
        seed_lineage=seed_lineage,
        seed_snapshots=seed_snapshots,
    )
    lineage["project_id"] = args.project_id
    snapshots = lineage.pop("relation_snapshots")
    write_json(project_dir / "lineage.json", lineage)
    write_json(project_dir / "horae_relation_snapshots.json", snapshots)
    task_ids = [item["task_id"] for item in lineage["nodes"]]
    collect_details_and_sql(project_dir, task_ids, set(roots), args.sleep, args.retries)

    # The root task's task card is the authoritative target for sparkIndex
    # tasks whose SQL body is exposed as a SELECT without an INSERT target.
    details = json.loads((project_dir / "task_details.json").read_text(encoding="utf-8"))
    targets = {str(item["task_id"]): str(item.get("target") or "") for item in details}
    for node in lineage["nodes"]:
        target = targets.get(str(node["task_id"]), "")
        if node.get("is_root") and target:
            node["task_name"] = target
    write_json(project_dir / "lineage.json", lineage)

    collect_runtime_logs(project_dir, details, args.sleep, args.retries, args.max_log_chars)
    py = sys.executable
    steps: list[dict[str, Any]] = []
    steps.append(
        run_step(
            "extract_hive_log_sql_facts",
            [py, str(SCRIPT_DIR / "extract_sql_facts.py"), str(project_dir), "--dialect", args.dialect, "--log-artifacts", "log_artifacts_full.json", "--prefix", "hive_log"],
            project_dir,
        )
    )
    steps.append(
        run_step(
            "extract_page_sql_facts",
            [py, str(SCRIPT_DIR / "extract_sql_facts.py"), str(project_dir), "--dialect", args.dialect, "--log-artifacts", "code_artifacts_page.json", "--prefix", "page"],
            project_dir,
        )
    )
    steps.append(
        run_step(
            "merge_strategy_sql_facts",
            [py, str(SCRIPT_DIR / "merge_strategy_sql_facts.py"), str(project_dir)],
            project_dir,
        )
    )
    steps.append(
        run_step(
            "build_graph_facts_initial",
            [py, str(SCRIPT_DIR / "build_graph_facts.py"), str(project_dir), "--prefix", "strategy"],
            project_dir,
        )
    )
    steps.append(
        run_step(
            "extract_column_lineage",
            [py, str(SCRIPT_DIR / "extract_column_lineage.py"), str(project_dir), "--prefix", "strategy", "--dialect", args.dialect],
            project_dir,
        )
    )
    steps.append(
        run_step(
            "build_graph_facts_final",
            [py, str(SCRIPT_DIR / "build_graph_facts.py"), str(project_dir), "--prefix", "strategy"],
            project_dir,
        )
    )
    for step_name, script_name in [
        ("audit_graph_facts", "audit_graph_facts.py"),
        ("export_neo4j_schema", "export_neo4j_schema.py"),
        ("export_neo4j_cypher", "export_neo4j_cypher.py"),
        ("export_query_templates", "export_query_templates.py"),
        ("validate_graph_queries", "validate_graph_queries.py"),
    ]:
        steps.append(
            run_step(
                step_name,
                [py, str(SCRIPT_DIR / script_name), str(project_dir), "--prefix", "strategy"],
                project_dir,
            )
        )

    field_output = args.field_output
    if args.field_target:
        field_output = field_output or str(project_dir / "field_paths.json")
        field_command = [
            py,
            str(SCRIPT_DIR / "field_path_consumer.py"),
            str(project_dir),
            "--target-dataset",
            args.field_target,
            "--prefix",
            "strategy",
            "--output",
            field_output,
        ]
        if args.fields:
            field_command.extend(["--fields", args.fields])
        steps.append(run_step("build_field_paths", field_command, project_dir))

    manifest = {
        "project_id": args.project_id,
        "project_dir": str(project_dir),
        "root_task_ids": roots,
        "collection_source": "opencli_horae",
        "reused_project": str(Path(args.reuse_project).resolve()) if args.reuse_project else None,
        "graph_prefix": "strategy",
        "runtime_logs_collected": True,
        "sz_metadata_collected": False,
        "steps": steps,
    }
    write_json(project_dir / "opencli_pipeline_manifest.json", manifest)
    print(json.dumps({"project_dir": str(project_dir), **lineage["summary"], "steps": len(steps), "field_output": field_output}, ensure_ascii=False))


if __name__ == "__main__":
    main()
