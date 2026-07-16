#!/usr/bin/env python3
"""Collect Horae upstream task lineage with breadth-first traversal.

The script stores detailed results locally and prints only aggregate summary.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path


HORAE_APP = Path("/Users/yuanchunzhang/Downloads/horae-api")
HORAE_ENV = Path("/Applications/personal-work/horae_cookie.env")


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
        value = value.replace("\\;", ";").replace("\\ ", " ")
        os.environ["HORAE_COOKIE"] = value
        return
    # Allow the installed Horae client to reuse its persisted authenticated session.
    return


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


def collect(
    root: str,
    max_depth: int,
    max_nodes: int,
    sleep_sec: float,
    retries: int,
    backoff_sec: float,
) -> dict:
    sys.path.insert(0, str(HORAE_APP))
    load_horae_cookie()

    from horae.client import HoraeAPI

    api = HoraeAPI()
    api.auth()

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
    visited_expansions: set[str] = set()
    frontier_by_depth: Counter[int] = Counter({0: 1})
    errors: list[dict] = []

    while queue:
        task_id, depth = queue.popleft()
        if task_id in visited_expansions or depth >= max_depth:
            continue
        if len(nodes) >= max_nodes:
            errors.append({"task_id": task_id, "error": "max_nodes_reached"})
            break
        visited_expansions.add(task_id)

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
                frontier_by_depth[depth + 1] += 1
                queue.append((up_id, depth + 1))
            edges.append(
                {
                    "from_task_id": up_id,
                    "to_task_id": task_id,
                    "relation": "UPSTREAM_OF",
                    "depth_from_root": depth + 1,
                }
            )
        if sleep_sec:
            time.sleep(sleep_sec)

    upstream_from = {edge["from_task_id"] for edge in edges}
    has_upstream = {edge["to_task_id"] for edge in edges}
    source_ids = sorted(task_id for task_id in nodes if task_id not in has_upstream)
    pending_retry_ids = {item["task_id"] for item in errors if item.get("status") == "pending_retry"}
    terminal_expanded_ids = sorted(
        task_id
        for task_id in visited_expansions
        if task_id not in has_upstream and task_id not in pending_retry_ids
    )

    return {
        "root_task_id": root,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "max_depth": max_depth,
        "nodes": sorted(nodes.values(), key=lambda item: (item.get("depth", 0), item["task_id"])),
        "edges": edges,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "expanded_count": len(visited_expansions),
            "max_observed_depth": max((n.get("depth", 0) for n in nodes.values()), default=0),
            "frontier_by_depth": dict(sorted(frontier_by_depth.items())),
            "layer_distribution": dict(Counter(n["layer"] for n in nodes.values())),
            "source_node_count": len(source_ids),
            "terminal_expanded_node_count": len(terminal_expanded_ids),
            "unexpanded_node_count": len(set(nodes) - visited_expansions),
            "upstream_node_count": len(upstream_from),
            "error_count": len(errors),
            "pending_retry_count": len(pending_retry_ids),
        },
        "source_task_ids": source_ids,
        "terminal_expanded_task_ids": terminal_expanded_ids,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-nodes", type=int, default=2000)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=0.5)
    parser.add_argument(
        "--output-dir",
        default="/Applications/personal-work/kg-code-snapshots/lineage",
    )
    args = parser.parse_args()

    result = collect(
        args.task_id,
        args.max_depth,
        args.max_nodes,
        args.sleep,
        retries=args.retries,
        backoff_sec=args.backoff,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{args.task_id}_upstream_lineage.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    print(json.dumps({"output_path": str(output_path), **result["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
