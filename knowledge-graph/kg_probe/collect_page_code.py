#!/usr/bin/env python3
"""Silently collect SQL/code embedded in Horae task pages.

Priority covered here:
- sparkIndex and similar tasks: SQL embedded in taskExtBase
- sync tasks: SQL snippets embedded in taskExtBase

The script never prints SQL content. It stores code artifacts on disk and emits
only aggregate counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

from collect_project import init_api  # noqa: E402


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return value[:160] or "code"


def task_table_name(html: str, task_id: str) -> str:
    m = re.search(r'"task_desc"\s*:\s*"([^"]+)"', html)
    if m and "." in m.group(1):
        return m.group(1)
    m = re.search(r'editTask\([^)]+\)">([^<]+)</a>', html)
    if m:
        return m.group(1).strip()
    return f"task_{task_id}"


def sql_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def collect_for_task(
    api,
    task_id: str,
    output_dir: Path,
    retries: int = 3,
    backoff_sec: float = 0.5,
) -> tuple[list[dict], dict | None]:
    from horae.commands.detail import is_valid_detail_html
    from horae.utils import extract_sql_from_extbase, extract_sync_sqls_from_extbase

    resp = None
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = api.get_task_detail(task_id)
            if resp.status_code != 200:
                raise RuntimeError(f"detail HTTP {resp.status_code}")
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                return [], {
                    "task_id": task_id,
                    "error": str(last_error),
                    "status": "pending_retry",
                    "retry_count": retries,
                }
            time.sleep(backoff_sec * (2**attempt))

    html = resp.text
    if not is_valid_detail_html(html, task_id):
        return [], {
            "task_id": task_id,
            "error": "invalid detail html while collecting page code",
            "status": "pending_retry",
            "retry_count": retries,
        }
    table_name = task_table_name(html, task_id)
    artifacts = []

    embedded = extract_sql_from_extbase(html)
    if embedded:
        labels = {"pre": "prepare.sqls", "query": "query.sql", "post": "finish.sqls"}
        for key, prop_name in labels.items():
            text = embedded.get(key, "")
            if not text.strip():
                continue
            hid = sql_hash(text)
            path = output_dir / f"{task_id}_{key}_{safe_filename(table_name)}_{hid}.sql"
            path.write_text(text)
            artifacts.append(
                {
                    "task_id": task_id,
                    "artifact_id": f"{task_id}_{key}_{hid}",
                    "source_system": "horae",
                    "source_type": "task_page_extbase_sql",
                    "prop_name": prop_name,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256_16": hid,
                    "table_name_hint": table_name,
                }
            )

    sync_sqls = extract_sync_sqls_from_extbase(html)
    if sync_sqls:
        for key, text in sync_sqls.items():
            if not text.strip():
                continue
            hid = sql_hash(text)
            path = output_dir / f"{task_id}_{key}_{safe_filename(table_name)}_{hid}.sql"
            path.write_text(text)
            artifacts.append(
                {
                    "task_id": task_id,
                    "artifact_id": f"{task_id}_{key}_{hid}",
                    "source_system": "horae",
                    "source_type": "task_page_sync_sql",
                    "prop_name": key,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256_16": hid,
                    "table_name_hint": table_name,
                }
            )

    return artifacts, None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--limit", type=int, default=0, help="0 means all tasks")
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

    code_dir = project_dir / "code" / "page"
    code_dir.mkdir(parents=True, exist_ok=True)
    artifacts_path = project_dir / "code_artifacts_page.json"
    errors_path = project_dir / "code_artifacts_page_errors.json"

    existing = []
    if artifacts_path.exists() and not args.force:
        existing = json.loads(artifacts_path.read_text())
    existing_by_task = {}
    for item in existing:
        existing_by_task.setdefault(item["task_id"], []).append(item)

    api = init_api()
    artifacts = list(existing)
    errors = []
    skipped = 0
    collected_tasks = 0
    tasks_with_code = 0
    artifact_count_before = len(artifacts)

    for task_id in task_ids:
        if task_id in existing_by_task and not args.force:
            skipped += 1
            continue
        task_artifacts, error = collect_for_task(
            api,
            task_id,
            code_dir,
            retries=args.retries,
            backoff_sec=args.backoff,
        )
        collected_tasks += 1
        if error:
            errors.append(error)
        if task_artifacts:
            tasks_with_code += 1
            artifacts.extend(task_artifacts)
        artifacts_path.write_text(json.dumps(artifacts, ensure_ascii=False, indent=2))
        errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2))
        if args.sleep:
            time.sleep(args.sleep)

    artifacts_path.write_text(json.dumps(artifacts, ensure_ascii=False, indent=2))
    errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2))

    print(
        json.dumps(
            {
                "task_count": len(task_ids),
                "collected_tasks": collected_tasks,
                "skipped_tasks": skipped,
                "tasks_with_code": tasks_with_code,
                "new_artifact_count": len(artifacts) - artifact_count_before,
                "total_artifact_count": len(artifacts),
                "error_count": len(errors),
                "artifacts_path": str(artifacts_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
