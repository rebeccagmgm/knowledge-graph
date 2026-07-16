#!/usr/bin/env python3
"""Collect a Horae task project snapshot for KG prototyping.

This collector keeps proprietary code/log content on disk and prints only
aggregate counts. It intentionally writes a replayable fact snapshot before
any Neo4j-specific modeling.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path


HORAE_APP = Path("/Users/yuanchunzhang/Downloads/horae-api")
HORAE_ENV = Path("/Applications/personal-work/horae_cookie.env")
DEFAULT_OUTPUT_ROOT = Path("/Applications/personal-work/kg-code-snapshots/projects")


def load_horae_cookie() -> None:
    if os.environ.get("HORAE_COOKIE"):
        return
    if not HORAE_ENV.exists():
        return

    text = HORAE_ENV.read_text(errors="ignore").strip()
    for line in text.splitlines():
        line = line.strip()
        if not line or "HORAE_COOKIE" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        if key.strip() != "HORAE_COOKIE":
            continue
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        else:
            try:
                value = shlex.split(value)[0]
            except ValueError:
                pass
        os.environ["HORAE_COOKIE"] = value.replace("\\;", ";").replace("\\ ", " ")
        return
    # The installed Horae client may already have a persisted authenticated session.
    # In that case HoraeAPI.auth() below performs the normal client-side lookup.
    return


def init_api():
    sys.path.insert(0, str(HORAE_APP))
    load_horae_cookie()
    from horae.client import HoraeAPI

    api = HoraeAPI()
    api.auth()
    return api


def layer_of(name: str) -> str:
    low = (name or "").lower()
    if low.startswith("odata") or ".odata" in low:
        return "odata"
    if low.startswith("pdata") or ".pdata" in low:
        return "pdata"
    if low.startswith("dm_index_n") or ".dm_index_n" in low:
        return "dm_index_n"
    if low.startswith("dm_") or low.startswith("dm.") or ".dm_" in low:
        return "dm"
    return "other"


def normalize_task(raw: dict) -> dict:
    task_id = str(raw.get("task_id") or raw.get("id") or "").strip()
    task_name = str(raw.get("task_name") or raw.get("name") or "").strip()
    return {
        "task_id": task_id,
        "task_name": task_name,
        "topic_name": raw.get("topic_name") or raw.get("topic") or "",
        "in_charge": raw.get("in_charge") or raw.get("owner") or "",
        "layer": layer_of(task_name),
    }


def fetch_direct_upstream(
    api,
    task_id: str,
    retries: int = 3,
    backoff_sec: float = 0.5,
) -> list[dict]:
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = api.get_downstream_list(task_id, is_downstream=0, hierarchy=1)
            if resp.status_code != 200:
                raise RuntimeError(f"Horae relation HTTP {resp.status_code} for {task_id}")
            try:
                data = json.loads(resp.text)
            except json.JSONDecodeError as exc:
                preview = re.sub(r"\s+", " ", resp.text[:240]).strip()
                raise RuntimeError(
                    f"Horae relation returned non-JSON for {task_id}; "
                    f"content_type={resp.headers.get('Content-Type', '')!r}; preview={preview!r}"
                ) from exc
            if not data.get("result"):
                raise RuntimeError(f"Horae relation failed for {task_id}: {data.get('message')}")
            return [normalize_task(item) for item in data.get("data", []) if item.get("task_id")]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(backoff_sec * (2**attempt))
    raise RuntimeError(str(last_error))


def collect_upstream_graph(
    api,
    root: str,
    max_depth: int,
    max_nodes: int,
    sleep_sec: float,
    retries: int = 3,
    backoff_sec: float = 0.5,
) -> dict:
    nodes: dict[str, dict] = {
        root: {
            "task_id": root,
            "task_name": "",
            "topic_name": "",
            "in_charge": "",
            "layer": "root_unknown",
            "depth": 0,
            "is_root": True,
        }
    }
    edges: list[dict] = []
    queue = deque([(root, 0)])
    expanded: set[str] = set()
    errors: list[dict] = []

    while queue:
        task_id, depth = queue.popleft()
        if task_id in expanded or depth >= max_depth:
            continue
        if len(nodes) >= max_nodes:
            errors.append({"task_id": task_id, "error": "max_nodes_reached"})
            break
        expanded.add(task_id)
        try:
            upstreams = fetch_direct_upstream(api, task_id, retries=retries, backoff_sec=backoff_sec)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "task_id": task_id,
                    "error": str(exc),
                    "status": "pending_retry",
                    "retry_count": retries,
                }
            )
            continue

        for up in upstreams:
            up_id = up["task_id"]
            if up_id not in nodes:
                up["depth"] = depth + 1
                up["is_root"] = False
                nodes[up_id] = up
                queue.append((up_id, depth + 1))
            edges.append(
                {
                    "from_task_id": up_id,
                    "to_task_id": task_id,
                    "relation": "UPSTREAM_OF",
                    "depth_from_root": depth + 1,
                    "source_system": "horae",
                    "source_type": "task_relation",
                }
            )
        if sleep_sec:
            time.sleep(sleep_sec)

    has_upstream = {edge["to_task_id"] for edge in edges}
    pending_retry_ids = {item["task_id"] for item in errors if item.get("status") == "pending_retry"}
    terminal_ids = sorted(
        task_id for task_id in expanded if task_id not in has_upstream and task_id not in pending_retry_ids
    )
    result_nodes = sorted(nodes.values(), key=lambda item: (item.get("depth", 0), item["task_id"]))
    return {
        "root_task_id": root,
        "nodes": result_nodes,
        "edges": edges,
        "terminal_task_ids": terminal_ids,
        "errors": errors,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "expanded_count": len(expanded),
            "unexpanded_count": len(set(nodes) - expanded),
            "max_observed_depth": max((n.get("depth", 0) for n in nodes.values()), default=0),
            "frontier_by_depth": dict(Counter(n.get("depth", 0) for n in nodes.values())),
            "layer_distribution": dict(Counter(n.get("layer", "unknown") for n in nodes.values())),
            "terminal_count": len(terminal_ids),
            "terminal_layer_distribution": dict(Counter(nodes[i]["layer"] for i in terminal_ids)),
            "error_count": len(errors),
            "pending_retry_count": len(pending_retry_ids),
        },
    }


def collect_details(api, task_ids: list[str], sleep_sec: float) -> tuple[list[dict], list[dict]]:
    from horae.commands.detail import extract_detail, is_valid_detail_html

    details = []
    errors = []
    for task_id in task_ids:
        try:
            resp = api.get_task_detail(task_id)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            if not is_valid_detail_html(resp.text, task_id):
                raise RuntimeError("invalid detail html")
            detail = extract_detail(resp.text, task_id)
            detail_norm = {
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
            }
            details.append(detail_norm)
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "error": str(exc)})
        if sleep_sec:
            time.sleep(sleep_sec)
    return details, errors


def run_date_matches(inst: dict, run_date: str | None) -> bool:
    if not run_date:
        return True
    return (inst.get("run_date") or inst.get("data_time") or "").startswith(run_date)


def is_success(inst: dict) -> bool:
    return "成功" in (inst.get("state") or "") or (inst.get("state") or "").upper() in {
        "SUCCESS",
        "SUCCESSFUL",
    }


def select_instance(instances: list[dict], run_date: str | None) -> dict | None:
    candidates = [inst for inst in instances if run_date_matches(inst, run_date)]
    successful = [inst for inst in candidates if is_success(inst)]
    if successful:
        return successful[0]
    return candidates[0] if candidates else None


def safe_log_name(task_id: str, inst: dict) -> str:
    raw_date = inst.get("run_date") or inst.get("data_time") or ""
    date = re.sub(r"\D", "", raw_date)[:8] or "latest"
    return f"{task_id}_{date}.log"


def collect_instances_and_logs(
    api,
    task_ids: list[str],
    output_dir: Path,
    run_date: str | None,
    log_scope: str,
    root_task_id: str,
    sleep_sec: float,
) -> tuple[list[dict], list[dict], list[dict]]:
    instance_facts = []
    log_facts = []
    errors = []
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    for task_id in task_ids:
        try:
            instances = list(api.get_instance_list(task_id))
            inst = select_instance(instances, run_date)
            if inst:
                instance_facts.append(
                    {
                        "task_id": task_id,
                        "selected": True,
                        "run_date": inst.get("run_date", ""),
                        "data_time": inst.get("data_time", ""),
                        "state": inst.get("state", ""),
                        "begin_time": inst.get("begin_time", ""),
                        "end_time": inst.get("end_time", ""),
                        "duration": inst.get("duration", ""),
                        "log_filename": inst.get("log_filename", ""),
                        "has_log_url": bool(inst.get("log_url")),
                    }
                )
            else:
                instance_facts.append({"task_id": task_id, "selected": False})

            should_fetch_log = log_scope == "all" or (
                log_scope == "root" and task_id == root_task_id
            )
            if should_fetch_log and inst and inst.get("log_url"):
                content = api.get_instance_log(inst["log_url"])
                if content is None:
                    raise RuntimeError("log content unavailable")
                log_path = logs_dir / safe_log_name(task_id, inst)
                log_path.write_text(content)
                low = content.lower()
                log_facts.append(
                    {
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
                )
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": task_id, "error": str(exc)})
        if sleep_sec:
            time.sleep(sleep_sec)

    return instance_facts, log_facts, errors


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-nodes", type=int, default=2000)
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=0.5)
    parser.add_argument("--detail-limit", type=int, default=0, help="0 means all tasks")
    parser.add_argument("--instance-limit", type=int, default=30, help="0 means all tasks")
    parser.add_argument("--log-scope", choices=["none", "root", "all"], default="root")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    args = parser.parse_args()

    api = init_api()
    collected_at = datetime.now().isoformat(timespec="seconds")
    project_dir = Path(args.output_root) / args.task_id
    project_dir.mkdir(parents=True, exist_ok=True)

    graph = collect_upstream_graph(
        api,
        args.task_id,
        args.max_depth,
        args.max_nodes,
        args.sleep,
        retries=args.retries,
        backoff_sec=args.backoff,
    )
    task_ids = [node["task_id"] for node in graph["nodes"]]
    write_json(project_dir / "lineage.json", graph)

    detail_ids = task_ids if args.detail_limit == 0 else task_ids[: args.detail_limit]
    details, detail_errors = collect_details(api, detail_ids, args.sleep)
    write_json(project_dir / "task_details.json", details)

    instance_ids = task_ids if args.instance_limit == 0 else task_ids[: args.instance_limit]
    instances, logs, log_errors = collect_instances_and_logs(
        api=api,
        task_ids=instance_ids,
        output_dir=project_dir,
        run_date=args.run_date,
        log_scope=args.log_scope,
        root_task_id=args.task_id,
        sleep_sec=args.sleep,
    )
    write_json(project_dir / "task_instances.json", instances)
    write_json(project_dir / "log_artifacts.json", logs)

    manifest = {
        "root_task_id": args.task_id,
        "run_date": args.run_date,
        "collected_at": collected_at,
        "project_dir": str(project_dir),
        "files": {
            "lineage": str(project_dir / "lineage.json"),
            "task_details": str(project_dir / "task_details.json"),
            "task_instances": str(project_dir / "task_instances.json"),
            "log_artifacts": str(project_dir / "log_artifacts.json"),
        },
        "summary": {
            **graph["summary"],
            "detail_collected_count": len(details),
            "detail_error_count": len(detail_errors),
            "instance_collected_count": len(instances),
            "log_collected_count": len(logs),
            "log_error_count": len(log_errors),
        },
        "errors": {
            "lineage": graph["errors"],
            "details": detail_errors,
            "logs": log_errors,
        },
    }
    write_json(project_dir / "manifest.json", manifest)
    print(json.dumps({"project_dir": str(project_dir), **manifest["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
