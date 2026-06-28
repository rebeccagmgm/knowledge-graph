#!/usr/bin/env python3
"""Collect Horae task details for an existing lineage snapshot."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

from collect_project import init_api  # noqa: E402


def collect_one_detail(api, task_id: str, retries: int = 3, backoff_sec: float = 0.5):
    from horae.commands.detail import extract_detail, is_valid_detail_html

    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = api.get_task_detail(task_id)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            if not is_valid_detail_html(resp.text, task_id):
                raise RuntimeError("invalid detail html")
            detail = extract_detail(resp.text, task_id)
            return {
                "task_id": task_id,
                "description": detail.get("描述", ""),
                "owners": detail.get("负责人", ""),
                "source": detail.get("源", ""),
                "task_type": detail.get("任务类型", ""),
                "topic": detail.get("主题", ""),
                "hive_db": detail.get("Hive库", ""),
                "cycle": detail.get("周期", ""),
                "cluster": detail.get("集群", ""),
                "priority": detail.get("优先级", ""),
                "retry_limit": detail.get("重试次数", ""),
                "script_path": detail.get("脚本路径", ""),
                "file_name": detail.get("文件名", ""),
                "sync_info": detail.get("_sync_info", {}),
                "source_system": "horae",
                "source_type": "task_detail",
            }, None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                return None, {
                    "task_id": task_id,
                    "error": str(last_error),
                    "status": "pending_retry",
                    "retry_count": retries,
                }
            time.sleep(backoff_sec * (2**attempt))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--limit", type=int, default=0, help="0 means all")
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=0.5)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    lineage = json.loads((project_dir / "lineage.json").read_text())
    task_ids = [node["task_id"] for node in lineage["nodes"]]
    if args.limit:
        task_ids = task_ids[: args.limit]

    output_path = project_dir / "task_details.json"
    errors_path = project_dir / "task_detail_errors.json"
    existing = []
    if output_path.exists() and not args.force:
        existing = json.loads(output_path.read_text())
    done = {item["task_id"] for item in existing}
    todo = [task_id for task_id in task_ids if task_id not in done]

    api = init_api()
    merged = list(existing)
    errors = []
    collected_count = 0
    for task_id in todo:
        detail, error = collect_one_detail(
            api,
            task_id,
            retries=args.retries,
            backoff_sec=args.backoff,
        )
        if detail:
            merged.append(detail)
            collected_count += 1
        if error:
            errors.append(error)
        output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
        errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2))
        if args.sleep:
            time.sleep(args.sleep)
    print(
        json.dumps(
            {
                "task_count": len(task_ids),
                "existing_count": len(existing),
                "collected_count": collected_count,
                "total_detail_count": len(merged),
                "error_count": len(errors),
                "details_path": str(output_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
