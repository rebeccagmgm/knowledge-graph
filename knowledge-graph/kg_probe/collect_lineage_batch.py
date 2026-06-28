#!/usr/bin/env python3
"""Collect upstream lineage summaries for multiple Horae root tasks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

from collect_project import collect_upstream_graph, init_api, write_json  # noqa: E402


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
    parser.add_argument("--tasks", default=None)
    parser.add_argument("--task-file", default=None)
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-nodes", type=int, default=3000)
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=0.5)
    parser.add_argument("--output-root", default=os.environ.get("KG_LINEAGE_ROOT", "artifacts/lineage_batch"))
    args = parser.parse_args()

    api = init_api()
    tasks = parse_tasks(args.tasks, args.task_file)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "lineage_batch_manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        summaries = existing.get("summaries", [])
    else:
        summaries = []
    done = {item.get("task_id") for item in summaries if item.get("path")}
    for task_id in tasks:
        path = output_root / f"{task_id}_lineage.json"
        if task_id not in done and path.exists():
            graph = json.loads(path.read_text())
            summaries.append({"task_id": task_id, "path": str(path), **graph.get("summary", {})})
            done.add(task_id)

    try:
        for task_id in tasks:
            if task_id in done:
                continue
            try:
                graph = collect_upstream_graph(
                    api,
                    task_id,
                    args.max_depth,
                    args.max_nodes,
                    args.sleep,
                    retries=args.retries,
                    backoff_sec=args.backoff,
                )
                path = output_root / f"{task_id}_lineage.json"
                write_json(path, graph)
                summary = {
                    "task_id": task_id,
                    "path": str(path),
                    **graph["summary"],
                }
            except Exception as exc:  # noqa: BLE001
                summary = {"task_id": task_id, "error": str(exc)}
            summaries.append(summary)
            manifest = {"task_count": len(tasks), "summaries": summaries}
            write_json(manifest_path, manifest)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        manifest = {"task_count": len(tasks), "summaries": summaries, "interrupted": True}
        write_json(manifest_path, manifest)
        raise

    manifest = {"task_count": len(tasks), "summaries": summaries}
    write_json(manifest_path, manifest)
    print(json.dumps({"manifest_path": str(manifest_path), "task_count": len(tasks)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
