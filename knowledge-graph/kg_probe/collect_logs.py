#!/usr/bin/env python3
"""Collect runtime logs for tasks from an existing project lineage snapshot."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

from collect_project import init_api, is_success, select_instance  # noqa: E402


def safe_log_name(task_id: str, inst: dict) -> str:
    raw_date = inst.get("run_date") or inst.get("data_time") or ""
    date = re.sub(r"\D", "", raw_date)[:8] or "latest"
    return f"{task_id}_{date}.log"


def load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--limit", type=int, default=0, help="0 means all")
    parser.add_argument(
        "--task-types",
        default="",
        help="Comma-separated task types from task_details.json, e.g. hiveTask,hiveTask-2.0",
    )
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    lineage = json.loads((project_dir / "lineage.json").read_text())
    task_ids = [node["task_id"] for node in lineage["nodes"]]
    if args.task_types:
        wanted = {item.strip() for item in args.task_types.split(",") if item.strip()}
        details_path = project_dir / "task_details.json"
        details = json.loads(details_path.read_text()) if details_path.exists() else []
        type_by_task = {item["task_id"]: item.get("task_type", "") for item in details}
        task_ids = [task_id for task_id in task_ids if type_by_task.get(task_id) in wanted]
    if args.limit:
        task_ids = task_ids[: args.limit]

    logs_dir = project_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    instances_path = project_dir / "task_instances_full.json"
    logs_path = project_dir / "log_artifacts_full.json"
    errors_path = project_dir / "log_collection_errors.json"

    existing_logs = load_existing(logs_path)
    existing_by_task = {item["task_id"]: item for item in existing_logs}
    instance_facts = load_existing(instances_path)
    errors = load_existing(errors_path)

    api = init_api()
    collected = 0
    skipped = 0
    missing_instance = 0
    missing_log = 0

    for task_id in task_ids:
        if not args.force and task_id in existing_by_task:
            skipped += 1
            continue
        try:
            instances = list(api.get_instance_list(task_id))
            inst = select_instance(instances, args.run_date)
            if not inst:
                missing_instance += 1
                instance_facts.append({"task_id": task_id, "selected": False})
                continue
            instance_facts.append(
                {
                    "task_id": task_id,
                    "selected": True,
                    "run_date": inst.get("run_date", ""),
                    "data_time": inst.get("data_time", ""),
                    "state": inst.get("state", ""),
                    "is_success": is_success(inst),
                    "begin_time": inst.get("begin_time", ""),
                    "end_time": inst.get("end_time", ""),
                    "duration": inst.get("duration", ""),
                    "log_filename": inst.get("log_filename", ""),
                    "has_log_url": bool(inst.get("log_url")),
                }
            )
            if not inst.get("log_url"):
                missing_log += 1
                continue
            content = api.get_instance_log(inst["log_url"])
            if content is None:
                missing_log += 1
                errors.append({"task_id": task_id, "error": "log content unavailable"})
                continue
            log_path = logs_dir / safe_log_name(task_id, inst)
            log_path.write_text(content)
            low = content.lower()
            fact = {
                "task_id": task_id,
                "path": str(log_path),
                "run_date": inst.get("run_date", ""),
                "bytes": log_path.stat().st_size,
                "line_count": content.count("\n") + 1,
                "insert_count": low.count("insert "),
                "select_count": low.count("select "),
                "from_count": low.count(" from "),
                "join_count": low.count(" join "),
            }
            existing_by_task[task_id] = fact
            collected += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "error": str(exc)})
        if args.sleep:
            time.sleep(args.sleep)

    logs = sorted(existing_by_task.values(), key=lambda item: item["task_id"])
    instances_path.write_text(json.dumps(instance_facts, ensure_ascii=False, indent=2))
    logs_path.write_text(json.dumps(logs, ensure_ascii=False, indent=2))
    errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2))

    print(
        json.dumps(
            {
                "task_count": len(task_ids),
                "collected": collected,
                "skipped": skipped,
                "missing_instance": missing_instance,
                "missing_log": missing_log,
                "total_log_artifacts": len(logs),
                "error_count": len(errors),
                "logs_path": str(logs_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
