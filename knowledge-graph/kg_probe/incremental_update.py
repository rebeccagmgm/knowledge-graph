#!/usr/bin/env python3
"""Registration-driven incremental scanner for project knowledge graphs.

The first version detects semantic changes cheaply and delegates publishing to
the existing full project pipeline. It intentionally keeps graph mutation out
of the scanner so a failed rebuild cannot partially update Neo4j.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import sqlglot
except ImportError:  # pragma: no cover - text normalization remains available
    sqlglot = None


DEFAULT_OUTPUT_ROOT = Path(os.environ.get("KG_OUTPUT_ROOT", "artifacts/projects"))
DEFAULT_LINEAGE_ROOT = Path(os.environ.get("KG_LINEAGE_ROOT", "artifacts/lineage_batch"))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def stable_hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_sql(text: str, dialect: str = "spark") -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if sqlglot is not None:
        logger = logging.getLogger("sqlglot")
        previous_level = logger.level
        try:
            logger.setLevel(logging.ERROR)
            statements = sqlglot.parse(text, read=dialect)
            return ";".join(
                statement.sql(dialect=dialect, pretty=False, normalize=True)
                for statement in statements
            )
        except Exception:  # noqa: BLE001
            pass
        finally:
            logger.setLevel(previous_level)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip().lower()


def read_artifact_text(item: dict, project_dir: Path) -> str:
    raw_path = item.get("statement_path") or item.get("path")
    if not raw_path:
        return item.get("sql", "")
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_dir / path
    if not path.exists():
        return item.get("sql", "")
    return path.read_text(errors="replace")


def task_code_fingerprints(project_dir: Path, dialect: str = "spark") -> dict[str, dict]:
    strategy_path = project_dir / "strategy_sql_statements.json"
    if strategy_path.exists():
        rows = load_json(strategy_path, [])
    else:
        rows = load_json(project_dir / "code_artifacts_page.json", [])
        rows += load_json(project_dir / "log_artifacts_full.json", [])

    grouped = defaultdict(list)
    for item in rows:
        task_id = str(item.get("task_id", ""))
        if not task_id:
            continue
        text = read_artifact_text(item, project_dir)
        grouped[task_id].append(
            {
                "source": item.get("strategy_source") or item.get("source_type") or "unknown",
                "name": item.get("prop_name") or item.get("statement_index") or item.get("artifact_id"),
                "raw": text,
                "semantic": normalize_sql(text, dialect),
            }
        )

    result = {}
    for task_id, items in grouped.items():
        items.sort(key=lambda x: (str(x["source"]), str(x["name"]), x["semantic"]))
        result[task_id] = {
            "statement_count": len(items),
            "raw_hash": stable_hash([(x["source"], x["name"], x["raw"]) for x in items]),
            "semantic_hash": stable_hash([(x["source"], x["name"], x["semantic"]) for x in items]),
        }
    return result


def schema_payload(project_dir: Path) -> list[dict]:
    rows = load_json(project_dir / "sz_metadata" / "dataset_dms.json", [])
    result = []
    for row in rows:
        exact = row.get("exact_records") or row.get("records") or []
        records = []
        for record in exact:
            columns = record.get("columns") or record.get("refColumns") or []
            records.append(
                {
                    "qualifiedName": record.get("qualifiedName"),
                    "dbName": record.get("dbName"),
                    "name": record.get("name"),
                    "comment": record.get("comment") or record.get("description"),
                    "columns": [
                        {
                            "name": col.get("name"),
                            "type": col.get("type") or col.get("dataType"),
                            "comment": col.get("comment") or col.get("description"),
                        }
                        for col in columns
                    ],
                }
            )
        result.append({"dataset": row.get("dataset"), "records": records})
    return sorted(result, key=lambda x: str(x["dataset"]))


def indicator_payload(project_dir: Path) -> list[dict]:
    rows = load_json(project_dir / "sz_metadata" / "indicator_registry.json", [])
    result = []
    for row in rows:
        records = row.get("exact_records") or row.get("records") or []
        result.append(
            {
                "dataset": row.get("dataset"),
                "records": [
                    {
                        key: record.get(key)
                        for key in sorted(record)
                        if key not in {"lastUpdateTime", "updateTime", "updated_at"}
                    }
                    for record in records
                ],
            }
        )
    return sorted(result, key=lambda x: str(x["dataset"]))


def build_snapshot(project_dir: Path, dialect: str = "spark") -> dict:
    lineage = load_json(project_dir / "lineage.json", {})
    details = load_json(project_dir / "task_details.json", [])
    detail_by_task = {str(item["task_id"]): item for item in details if item.get("task_id")}
    code_by_task = task_code_fingerprints(project_dir, dialect)
    task_ids = sorted({str(item["task_id"]) for item in lineage.get("nodes", [])})
    edges = sorted(
        (str(item["from_task_id"]), str(item["to_task_id"]))
        for item in lineage.get("edges", [])
    )
    task_snapshots = {}
    for task_id in task_ids:
        detail = detail_by_task.get(task_id, {})
        code = code_by_task.get(task_id, {})
        task_snapshots[task_id] = {
            "metadata_hash": stable_hash(detail),
            "code_raw_hash": code.get("raw_hash"),
            "code_semantic_hash": code.get("semantic_hash"),
            "statement_count": code.get("statement_count", 0),
            "task_type": detail.get("task_type"),
        }
    schemas = schema_payload(project_dir)
    indicators = indicator_payload(project_dir)
    return {
        "version": 1,
        "project_id": lineage.get("project_id") or project_dir.name,
        "generated_at": now_iso(),
        "root_task_ids": sorted(str(x) for x in lineage.get("root_task_ids", [])),
        "task_count": len(task_ids),
        "edge_count": len(edges),
        "task_ids_hash": stable_hash(task_ids),
        "dependency_hash": stable_hash(edges),
        "edges": [list(edge) for edge in edges],
        "tasks": task_snapshots,
        "dataset_schema_count": len(schemas),
        "dataset_schema_hash": stable_hash(schemas),
        "indicator_registry_count": len(indicators),
        "indicator_registry_hash": stable_hash(indicators),
    }


def downstream_closure(seed_ids: set[str], edges: list[list[str]]) -> list[str]:
    downstream = defaultdict(list)
    for upstream, child in edges:
        downstream[str(upstream)].append(str(child))
    seen = set(seed_ids)
    queue = deque(seed_ids)
    while queue:
        task_id = queue.popleft()
        for child in downstream.get(task_id, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return sorted(seen)


def compare_snapshots(old: dict, new: dict) -> dict:
    old_tasks = old.get("tasks", {})
    new_tasks = new.get("tasks", {})
    old_ids = set(old_tasks)
    new_ids = set(new_tasks)
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    metadata_changed = []
    code_changed = []
    text_only_changed = []
    for task_id in sorted(old_ids & new_ids):
        before, after = old_tasks[task_id], new_tasks[task_id]
        if before.get("metadata_hash") != after.get("metadata_hash"):
            metadata_changed.append(task_id)
        if before.get("code_semantic_hash") != after.get("code_semantic_hash"):
            code_changed.append(task_id)
        elif before.get("code_raw_hash") != after.get("code_raw_hash"):
            text_only_changed.append(task_id)

    old_edges = {tuple(x) for x in old.get("edges", [])}
    new_edges = {tuple(x) for x in new.get("edges", [])}
    edge_added = sorted([list(x) for x in new_edges - old_edges])
    edge_removed = sorted([list(x) for x in old_edges - new_edges])
    dependency_tasks = {str(x) for edge in edge_added + edge_removed for x in edge}
    seeds = set(added + removed + metadata_changed + code_changed) | dependency_tasks
    affected_tasks = downstream_closure(seeds, new.get("edges", []))
    schema_changed = old.get("dataset_schema_hash") != new.get("dataset_schema_hash")
    indicator_changed = old.get("indicator_registry_hash") != new.get("indicator_registry_hash")
    semantic_change = bool(seeds or schema_changed or indicator_changed)
    return {
        "semantic_change": semantic_change,
        "added_task_ids": added,
        "removed_task_ids": removed,
        "metadata_changed_task_ids": metadata_changed,
        "code_changed_task_ids": code_changed,
        "text_only_changed_task_ids": text_only_changed,
        "dependency_edges_added": edge_added,
        "dependency_edges_removed": edge_removed,
        "dataset_schema_changed": schema_changed,
        "indicator_registry_changed": indicator_changed,
        "affected_task_ids": affected_tasks,
    }


def affected_metric_ids(project_dir: Path, affected_tasks: set[str], prefix: str = "strategy") -> list[str]:
    nodes = load_jsonl(project_dir / f"{prefix}_graph_nodes.jsonl")
    edges = load_jsonl(project_dir / f"{prefix}_graph_edges.jsonl")
    metric_ids = {
        item["id"]: str(item.get("properties", {}).get("metric_id", ""))
        for item in nodes
        if "Metric" in item.get("labels", [])
    }
    task_nodes = {
        item["id"]: str(item.get("properties", {}).get("task_id", ""))
        for item in nodes
        if "ScheduleTask" in item.get("labels", [])
    }
    dataset_nodes = {
        item["id"]
        for item in nodes
        if "Dataset" in item.get("labels", [])
    }
    affected_task_nodes = {node_id for node_id, task_id in task_nodes.items() if task_id in affected_tasks}
    affected_datasets = {
        edge["to"]
        for edge in edges
        if edge.get("type") == "PRODUCES" and edge.get("from") in affected_task_nodes
    }
    result = set()
    for edge in edges:
        if edge.get("type") == "COMPUTED_BY" and edge.get("to") in affected_task_nodes:
            result.add(metric_ids.get(edge.get("from"), ""))
        if edge.get("type") == "STORED_IN" and edge.get("to") in affected_datasets:
            result.add(metric_ids.get(edge.get("from"), ""))
    return sorted(x for x in result if x)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def mark_manual_overrides(project_dir: Path, metric_ids: set[str], detected_at: str) -> int:
    path = project_dir / "manual_metric_overrides.json"
    rows = load_json(path, [])
    changed = 0
    for row in rows:
        if str(row.get("metric_id")) in metric_ids:
            if not row.get("needs_review"):
                changed += 1
            row["needs_review"] = True
            row["review_reason"] = "related_task_or_code_changed"
            row["review_requested_at"] = detected_at
    if rows:
        write_json(path, rows)
    return changed


def run_command(name: str, cmd: list[str], dry_run: bool = False) -> dict:
    if dry_run:
        return {"step": name, "returncode": 0, "dry_run": True, "cmd": cmd}
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    result = {
        "step": name,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout.splitlines()[-3:],
        "stderr_tail": proc.stderr.splitlines()[-3:],
    }
    if proc.returncode != 0:
        raise RuntimeError(f"{name} failed: {result['stderr_tail']}")
    return result


def validate_refresh_quality(project_dir: Path) -> dict:
    lineage = load_json(project_dir / "lineage.json", {})
    nodes = lineage.get("nodes", [])
    details = load_json(project_dir / "task_details.json", [])
    detail_errors = load_json(project_dir / "task_detail_errors.json", [])
    page_errors = load_json(project_dir / "code_artifacts_page_errors.json", [])
    log_errors = load_json(project_dir / "log_collection_errors.json", [])
    logs = load_json(project_dir / "log_artifacts_full.json", [])
    hive_task_ids = {
        str(item.get("task_id"))
        for item in details
        if item.get("task_type") in {"hiveTask", "hiveTask-2.0"}
    }
    log_task_ids = {str(item.get("task_id")) for item in logs}
    checks = {
        "missing_root_count": len(lineage.get("missing_root_task_ids", [])),
        "lineage_error_count": len(lineage.get("errors", [])),
        "missing_detail_count": max(len(nodes) - len(details), 0),
        "detail_error_count": len(detail_errors),
        "page_code_error_count": len(page_errors),
        "log_error_count": len(log_errors),
        "missing_hive_log_count": len(hive_task_ids - log_task_ids),
    }
    return {"ok": not any(checks.values()), **checks}


def refresh_inputs(config: dict, project_dir: Path, output_root: Path, lineage_root: Path, dry_run=False) -> list[dict]:
    script_dir = Path(__file__).parent
    py = sys.executable
    project_id = config["project_id"]
    tasks = [str(x) for x in config.get("result_task_ids", []) + config.get("supplemental_task_ids", [])]
    options = config.get("options", {})
    project_lineage_root = lineage_root / project_id
    commands = [
        ("refresh_lineage", [py, str(script_dir / "collect_lineage_batch.py"), "--tasks", ",".join(tasks), "--max-depth", str(options.get("max_depth", 25)), "--max-nodes", str(options.get("max_nodes", 3000)), "--output-root", str(project_lineage_root), "--force"]),
        ("merge_lineage", [py, str(script_dir / "merge_lineage_project.py"), "--project-id", project_id, "--tasks", ",".join(tasks), "--lineage-root", str(project_lineage_root), "--output-root", str(output_root)]),
        ("refresh_details", [py, str(script_dir / "collect_details.py"), str(project_dir), "--force"]),
        ("refresh_page_code", [py, str(script_dir / "collect_page_code.py"), str(project_dir), "--force"]),
        ("refresh_hive_logs", [py, str(script_dir / "collect_logs.py"), str(project_dir), "--task-types", "hiveTask,hiveTask-2.0", "--force"]),
        ("parse_hive_sql", [py, str(script_dir / "extract_sql_facts.py"), str(project_dir), "--dialect", options.get("dialect", "spark"), "--log-artifacts", "log_artifacts_full.json", "--prefix", "hive_log"]),
        ("parse_page_sql", [py, str(script_dir / "extract_sql_facts.py"), str(project_dir), "--dialect", options.get("dialect", "spark"), "--log-artifacts", "code_artifacts_page.json", "--prefix", "page"]),
        ("merge_sql_strategy", [py, str(script_dir / "merge_strategy_sql_facts.py"), str(project_dir)]),
    ]
    return [run_command(name, cmd, dry_run) for name, cmd in commands]


def rebuild_project(config: dict, output_root: Path, lineage_root: Path, dry_run=False) -> dict:
    script = Path(__file__).with_name("run_project_pipeline.py")
    options = config.get("options", {})
    tasks = [str(x) for x in config.get("result_task_ids", []) + config.get("supplemental_task_ids", [])]
    cmd = [
        sys.executable,
        str(script),
        "--project-id", config["project_id"],
        "--tasks", ",".join(tasks),
        "--output-root", str(output_root),
        "--lineage-root", str(lineage_root / config["project_id"]),
        "--max-depth", str(options.get("max_depth", 25)),
        "--max-nodes", str(options.get("max_nodes", 3000)),
    ]
    if options.get("build_llm"):
        cmd.extend(["--build-llm", "--llm-provider", options.get("llm_provider", "mock")])
        if options.get("llm_model"):
            cmd.extend(["--llm-model", options["llm_model"]])
    if options.get("import_neo4j"):
        cmd.append("--import-neo4j")
    return run_command("rebuild_project", cmd, dry_run)


def find_project(registry: dict, project_id: str) -> dict:
    for project in registry.get("projects", []):
        if project.get("project_id") == project_id:
            return project
    raise SystemExit(f"Project not found in registry: {project_id}")


def is_due(state: dict, interval_hours: int) -> bool:
    last = state.get("last_scan_at")
    if not last:
        return True
    try:
        parsed = datetime.fromisoformat(last)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= parsed.astimezone(timezone.utc) + timedelta(hours=interval_hours)
    except ValueError:
        return True


@contextlib.contextmanager
def project_lock(project_dir: Path):
    lock_path = project_dir / "incremental" / "scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Incremental scan already running: {project_dir.name}") from exc
        handle.write(f"pid={os.getpid()} started_at={now_iso()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def scan_project(config: dict, args) -> dict:
    project_id = config["project_id"]
    project_dir = Path(args.output_root) / project_id
    incremental_dir = project_dir / "incremental"
    snapshot_path = incremental_dir / "current_snapshot.json"
    state_path = incremental_dir / "state.json"
    state = load_json(state_path, {})
    interval = int(config.get("scan_interval_hours", 48))
    if not args.force_scan and not is_due(state, interval):
        result = {"project_id": project_id, "status": "not_due", "last_scan_at": state.get("last_scan_at")}
        print(json.dumps(result, ensure_ascii=False))
        return result

    old_snapshot = load_json(snapshot_path, None)
    if old_snapshot is None:
        old_snapshot = build_snapshot(project_dir, args.dialect)
        write_json(snapshot_path, old_snapshot)

    refresh_steps = []
    candidate_dir = Path(args.candidate_project_dir) if args.candidate_project_dir else project_dir
    if not args.offline and not args.candidate_project_dir:
        try:
            refresh_steps = refresh_inputs(
                config,
                project_dir,
                Path(args.output_root),
                Path(args.lineage_root),
                args.dry_run,
            )
        except RuntimeError as exc:
            attempted_at = now_iso()
            failure = {
                "project_id": project_id,
                "status": "refresh_failed",
                "attempted_at": attempted_at,
                "quality": {"ok": False, "execution_error": str(exc)},
                "refresh_steps": refresh_steps,
            }
            failure_path = incremental_dir / "failures" / f"{attempted_at.replace(':', '').replace('+', '_')}.json"
            write_json(failure_path, failure)
            state["last_attempt_at"] = attempted_at
            state["last_attempt_status"] = "refresh_failed"
            state["last_failure_path"] = str(failure_path)
            write_json(state_path, state)
            result = {**failure, "failure_path": str(failure_path)}
            print(json.dumps(result, ensure_ascii=False))
            return result
        if args.dry_run:
            result = {"project_id": project_id, "status": "dry_run", "steps": refresh_steps}
            print(json.dumps(result, ensure_ascii=False))
            return result
        refresh_quality = validate_refresh_quality(project_dir)
        if not refresh_quality["ok"]:
            attempted_at = now_iso()
            failure = {
                "project_id": project_id,
                "status": "refresh_failed",
                "attempted_at": attempted_at,
                "quality": refresh_quality,
                "refresh_steps": refresh_steps,
            }
            failure_path = incremental_dir / "failures" / f"{attempted_at.replace(':', '').replace('+', '_')}.json"
            write_json(failure_path, failure)
            state["last_attempt_at"] = attempted_at
            state["last_attempt_status"] = "refresh_failed"
            state["last_failure_path"] = str(failure_path)
            write_json(state_path, state)
            result = {**failure, "failure_path": str(failure_path)}
            print(json.dumps(result, ensure_ascii=False))
            return result
    else:
        refresh_quality = {"ok": True, "mode": "offline"}

    new_snapshot = build_snapshot(candidate_dir, args.dialect)
    change = compare_snapshots(old_snapshot, new_snapshot)
    detected_at = now_iso()
    affected_metrics = affected_metric_ids(
        project_dir,
        set(change["affected_task_ids"]),
        args.graph_prefix,
    )
    change.update(
        {
            "project_id": project_id,
            "detected_at": detected_at,
            "old_snapshot_generated_at": old_snapshot.get("generated_at"),
            "new_snapshot_generated_at": new_snapshot.get("generated_at"),
            "affected_metric_ids": affected_metrics,
            "refresh_steps": refresh_steps,
            "refresh_quality": refresh_quality,
        }
    )
    change_id = detected_at.replace(":", "").replace("+", "_")
    change_path = incremental_dir / "changes" / f"{change_id}.json"
    write_json(change_path, change)

    marked = 0
    rebuild = None
    if change["semantic_change"]:
        marked = mark_manual_overrides(project_dir, set(affected_metrics), detected_at)
        if not args.no_rebuild and not args.offline and not args.candidate_project_dir:
            rebuild = rebuild_project(config, Path(args.output_root), Path(args.lineage_root), args.dry_run)
            new_snapshot = build_snapshot(project_dir, args.dialect)

    write_json(snapshot_path, new_snapshot)
    state = {
        "project_id": project_id,
        "last_scan_at": detected_at,
        "last_success_at": now_iso(),
        "last_change_path": str(change_path),
        "last_semantic_change": change["semantic_change"],
        "last_attempt_status": "success",
    }
    write_json(state_path, state)
    result = {
        "project_id": project_id,
        "status": "changed" if change["semantic_change"] else "unchanged",
        "change_path": str(change_path),
        "affected_task_count": len(change["affected_task_ids"]),
        "affected_metric_count": len(affected_metrics),
        "manual_overrides_marked": marked,
        "rebuild": rebuild,
    }
    print(json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default=os.environ.get("KG_PROJECT_REGISTRY", "project_registry.json"))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--project-id")
    target.add_argument("--all", action="store_true", help="Scan every enabled project in the registry")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--lineage-root", default=str(DEFAULT_LINEAGE_ROOT))
    parser.add_argument("--dialect", default="spark")
    parser.add_argument("--graph-prefix", default="strategy")
    parser.add_argument("--offline", action="store_true", help="Compare existing artifacts without internal service calls")
    parser.add_argument("--candidate-project-dir", default=None, help="Compare a candidate artifact directory in offline tests")
    parser.add_argument("--force-scan", action="store_true")
    parser.add_argument("--no-rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--initialize", action="store_true", help="Only write the current baseline snapshot")
    args = parser.parse_args()

    registry = load_json(Path(args.registry), {})
    configs = (
        [project for project in registry.get("projects", []) if project.get("enabled", True)]
        if args.all
        else [find_project(registry, args.project_id)]
    )
    if args.initialize:
        for config in configs:
            project_dir = Path(args.output_root) / config["project_id"]
            snapshot = build_snapshot(project_dir, args.dialect)
            path = project_dir / "incremental" / "current_snapshot.json"
            write_json(path, snapshot)
            result = {"project_id": config["project_id"], "status": "initialized", "snapshot_path": str(path), "task_count": snapshot["task_count"]}
            print(json.dumps(result, ensure_ascii=False))
        return
    results = []
    for config in configs:
        project_dir = Path(args.output_root) / config["project_id"]
        with project_lock(project_dir):
            results.append(scan_project(config, args))
    if args.all:
        print(json.dumps({"project_count": len(results), "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
