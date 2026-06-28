#!/usr/bin/env python3
"""Collect SzConnector metadata for project datasets and indicators."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path


SZ_APP = Path("/Users/yuanchunzhang/Downloads/szconnector")
SZ_ENV = Path(os.environ.get("SZCONNECTOR_ENV_FILE", ".env"))


def load_sz_env() -> None:
    if os.environ.get("SZCONNECTOR_COOKIE") and os.environ.get("SZCONNECTOR_TOKEN"):
        return
    if not SZ_ENV.exists():
        raise SystemExit(f"Missing env file: {SZ_ENV}")
    for line in SZ_ENV.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in {"SZCONNECTOR_COOKIE", "SZCONNECTOR_TOKEN"}:
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
        os.environ[key] = value.replace("\\;", ";").replace("\\ ", " ")


def init_client():
    sys.path.insert(0, str(SZ_APP))
    load_sz_env()
    from szconnector.core import AuthManager, ConfigManager, GfClient

    config = ConfigManager()
    auth = AuthManager(config)
    cookie = os.environ.get("SZCONNECTOR_COOKIE", "")
    token = os.environ.get("SZCONNECTOR_TOKEN", "")
    if cookie:
        auth._cookies = {}
        for part in cookie.split(";"):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                auth._cookies[key.strip()] = value.strip()
    if token:
        auth._token = token
    return GfClient(auth, config)


def split_dataset(dataset: str) -> tuple[str, str]:
    if "." not in dataset:
        return "", dataset
    db, table = dataset.split(".", 1)
    return db, table


def request_with_retry(fn, retries: int, backoff: float):
    last_error = None
    for attempt in range(retries + 1):
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries:
                return None, str(last_error)
            time.sleep(backoff * (2**attempt))
    return None, str(last_error)


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def dms_search(client, dataset: str, size: int) -> dict:
    from szconnector.plugins.datamapsearch import DataMapSearchPlugin

    plugin = DataMapSearchPlugin(client)
    return plugin.execute(
        {
            "keyword": dataset,
            "page": 1,
            "size": size,
            "type": "003000",
            "extra_database_id": "",
        }
    )


def indicator_search(client, dataset: str, size: int) -> dict:
    from szconnector.plugins.indicator import IndicatorPlugin

    db, table = split_dataset(dataset)
    plugin = IndicatorPlugin(client)
    return plugin.execute(
        {
            "keyword": table or dataset,
            "page": 1,
            "size": size,
            "db": db,
        }
    )


def normalize_dms(dataset: str, result: dict) -> dict:
    data = result.get("data", {}) if result else {}
    records = data.get("records", []) or []
    db, table = split_dataset(dataset)
    exact = []
    for rec in records:
        qn = (rec.get("qualifiedName") or "").lower()
        name = (rec.get("name") or "").lower()
        rec_db = (rec.get("dbName") or "").lower()
        if (
            qn.split("@", 1)[0] == dataset.lower()
            or (name == table.lower() and (not db or rec_db == db.lower()))
        ):
            exact.append(rec)
    return {
        "dataset": dataset,
        "total": data.get("totalResultNum", 0),
        "record_count": len(records),
        "exact_count": len(exact),
        "records": records,
        "exact_records": exact,
    }


def normalize_indicator(dataset: str, result: dict) -> dict:
    data = result.get("data", {}) if result else {}
    records = data.get("records", []) or []
    db, table = split_dataset(dataset)
    exact = []
    for rec in records:
        rec_db = (rec.get("dbName") or "").lower()
        rec_table = (rec.get("engTblName") or "").lower()
        if rec_db == db.lower() and rec_table == table.lower():
            exact.append(rec)
    return {
        "dataset": dataset,
        "total": data.get("totalResultNum", 0),
        "record_count": len(records),
        "exact_count": len(exact),
        "records": records,
        "exact_records": exact,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--limit", type=int, default=0, help="0 means all datasets")
    parser.add_argument("--from-graph", action="store_true", help="Read datasets from graph_nodes JSONL")
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=0.5)
    parser.add_argument("--flush-every", type=int, default=25)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    if args.from_graph:
        datasets = []
        graph_nodes_path = project_dir / f"{args.prefix}_graph_nodes.jsonl"
        for line in graph_nodes_path.read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if "Dataset" in item.get("labels", []):
                props = item.get("properties", {})
                datasets.append(
                    {
                        "dataset": props.get("name") or item["id"].removeprefix("dataset:"),
                        "layer": props.get("layer") or "other",
                    }
                )
    else:
        datasets = json.loads((project_dir / f"{args.prefix}_datasets.json").read_text())
    if args.limit:
        datasets = datasets[: args.limit]

    out_dir = project_dir / "sz_metadata"
    out_dir.mkdir(parents=True, exist_ok=True)
    dms_path = out_dir / "dataset_dms.json"
    indicator_path = out_dir / "indicator_registry.json"
    errors_path = out_dir / "sz_metadata_errors.json"

    existing_dms = json.loads(dms_path.read_text()) if dms_path.exists() else []
    existing_ind = json.loads(indicator_path.read_text()) if indicator_path.exists() else []
    errors = json.loads(errors_path.read_text()) if errors_path.exists() else []
    dms_done = {item["dataset"] for item in existing_dms}
    ind_done = {item["dataset"] for item in existing_ind}

    client = init_client()
    dms_results = list(existing_dms)
    indicator_results = list(existing_ind)
    dms_new = 0
    ind_new = 0
    dirty_dms = False
    dirty_ind = False
    dirty_errors = False

    for item in datasets:
        dataset = item["dataset"]
        if dataset not in dms_done:
            result, error = request_with_retry(
                lambda dataset=dataset: dms_search(client, dataset, args.size),
                args.retries,
                args.backoff,
            )
            if error:
                errors.append({"dataset": dataset, "source": "dms", "error": error})
                dirty_errors = True
            else:
                dms_results.append(normalize_dms(dataset, result))
                dms_new += 1
                dms_done.add(dataset)
                dirty_dms = True
            if args.flush_every and dms_new % args.flush_every == 0:
                if dirty_dms:
                    write_json_atomic(dms_path, dms_results)
                    dirty_dms = False
                if dirty_errors:
                    write_json_atomic(errors_path, errors)
                    dirty_errors = False

        if item.get("layer") == "dm_index_n" and dataset not in ind_done:
            result, error = request_with_retry(
                lambda dataset=dataset: indicator_search(client, dataset, args.size),
                args.retries,
                args.backoff,
            )
            if error:
                if "找不到任务" in error:
                    indicator_results.append(
                        {
                            "dataset": dataset,
                            "total": 0,
                            "record_count": 0,
                            "exact_count": 0,
                            "records": [],
                            "exact_records": [],
                            "status": "no_registry",
                            "error": error,
                        }
                    )
                    ind_new += 1
                    ind_done.add(dataset)
                    dirty_ind = True
                else:
                    errors.append({"dataset": dataset, "source": "indicator", "error": error})
                    dirty_errors = True
            else:
                indicator_results.append(normalize_indicator(dataset, result))
                ind_new += 1
                ind_done.add(dataset)
                dirty_ind = True
            if args.flush_every and ind_new % args.flush_every == 0:
                if dirty_ind:
                    write_json_atomic(indicator_path, indicator_results)
                    dirty_ind = False
                if dirty_errors:
                    write_json_atomic(errors_path, errors)
                    dirty_errors = False

        if args.sleep:
            time.sleep(args.sleep)

    if dirty_dms:
        write_json_atomic(dms_path, dms_results)
    if dirty_ind:
        write_json_atomic(indicator_path, indicator_results)
    if dirty_errors:
        write_json_atomic(errors_path, errors)

    summary = {
        "dataset_count": len(datasets),
        "dms_total": len(dms_results),
        "dms_new": dms_new,
        "dms_exact_count": sum(1 for item in dms_results if item.get("exact_count", 0) > 0),
        "indicator_total": len(indicator_results),
        "indicator_new": ind_new,
        "indicator_exact_count": sum(1 for item in indicator_results if item.get("exact_count", 0) > 0),
        "error_count": len(errors),
        "output_dir": str(out_dir),
    }
    write_json_atomic(out_dir / "sz_metadata_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
