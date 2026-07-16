#!/usr/bin/env python3
"""Run project collection for a list of Horae root task IDs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default=None, help="Comma-separated task IDs")
    parser.add_argument("--task-file", default=None)
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-nodes", type=int, default=3000)
    parser.add_argument("--detail-limit", type=int, default=0)
    parser.add_argument("--instance-limit", type=int, default=30)
    parser.add_argument("--log-scope", choices=["none", "root", "all"], default="root")
    parser.add_argument("--output-root", default="/Applications/personal-work/kg-code-snapshots/projects")
    args = parser.parse_args()

    tasks = parse_tasks(args.tasks, args.task_file)
    script = Path(__file__).with_name("collect_project.py")
    results = []
    for task_id in tasks:
        cmd = [
            sys.executable,
            str(script),
            task_id,
            "--max-depth",
            str(args.max_depth),
            "--max-nodes",
            str(args.max_nodes),
            "--detail-limit",
            str(args.detail_limit),
            "--instance-limit",
            str(args.instance_limit),
            "--log-scope",
            args.log_scope,
            "--output-root",
            args.output_root,
        ]
        if args.run_date:
            cmd.extend(["--run-date", args.run_date])
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        result = {
            "task_id": task_id,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout.strip().splitlines()[-1:] if proc.stdout.strip() else [],
            "stderr_tail": proc.stderr.strip().splitlines()[-3:] if proc.stderr.strip() else [],
        }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    batch_path = output_root / "batch_manifest.json"
    batch_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps({"batch_manifest": str(batch_path), "task_count": len(tasks)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
