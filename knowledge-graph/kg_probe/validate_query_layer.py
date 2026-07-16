#!/usr/bin/env python3
"""Run a compact real-Neo4j smoke suite for the query primitives."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


for local_path in [
    Path("/Applications/personal-work/kg-python-userbase/lib/python/site-packages"),
    Path("/Applications/personal-work/kg-python-userbase/lib/python3.9/site-packages"),
]:
    if local_path.exists():
        sys.path.insert(0, str(local_path))

from query_layer.neo4j_store import Neo4jStore  # noqa: E402
from query_layer.service import QueryService  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="trial_project")
    parser.add_argument("--project-dir", default="/Applications/personal-work/kg-code-snapshots/projects/trial_project")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password-file", default="/Applications/personal-work/kg-code-snapshots/neo4j_password.txt")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    store = Neo4jStore.from_password_file(args.uri, args.user, args.password_file)
    service = QueryService(store, args.project_id, args.project_dir)
    cases = [
        ("search_entities", {"query": "打新次数", "entity_types": ["metric"], "limit": 5}),
        ("resolve_entity", {"query": "152285", "entity_type": "schedule_task"}),
        ("get_metric_context", {"metric_id": "ind2023030962774756"}),
        ("get_task_context", {"task_id": "152285", "include_evidence": False}),
        ("get_dataset_context", {"query": "dm_index_n.index_grp_client_secu_purch_new_num", "entity_type": "dataset"}),
        ("find_definition_issues", {"issue_types": ["conflict"], "limit": 5}),
    ]
    results = []
    try:
        column_search = service.execute("search_entities", {"query": "running_phase", "entity_types": ["column"], "limit": 5})
        column_id = column_search["entities"][0]["entity_id"] if column_search["entities"] else None
        cases.insert(2, ("get_column_context", {"entity_id": column_id}))
        cases.extend([
            ("trace_upstream", {"subject": {"entity_type": "schedule_task", "key": "152285"}, "max_hops": 5, "limit": 10}),
            ("trace_downstream", {"entity_id": column_id, "max_hops": 5, "limit": 10}),
            ("analyze_impact", {"entity_id": column_id, "change_type": "drop", "max_hops": 20, "limit": 100, "include_sql_fallback": True}),
            ("compare_metric_definitions", {"metric_id": "ind2023030962774756"}),
        ])
        downstream_result = None
        for primitive, payload in cases:
            started = time.time()
            result = service.execute(primitive, payload)
            if primitive == "trace_downstream":
                downstream_result = result
            results.append({
                "primitive": primitive,
                "status": result["status"],
                "answer": result["answer"],
                "entity_count": len(result["entities"]),
                "path_count": len(result["paths"]),
                "evidence_count": len(result["evidence"]),
                "warning_codes": [item["code"] for item in result["warnings"]],
                "elapsed_seconds": round(time.time() - started, 3),
            })
        if downstream_result and downstream_result["paths"]:
            target_id = downstream_result["paths"][0]["nodes"][-1]
            result = service.execute("explain_lineage_path", {"from_entity_id": column_id, "to_entity_id": target_id, "max_hops": 10})
            results.append({"primitive": "explain_lineage_path", "status": result["status"], "answer": result["answer"], "entity_count": len(result["entities"]), "path_count": len(result["paths"]), "evidence_count": len(result["evidence"]), "warning_codes": [item["code"] for item in result["warnings"]]})
    finally:
        store.close()

    report = {
        "project_id": args.project_id,
        "primitive_count": len(results),
        "error_count": sum(item["status"] == "error" for item in results),
        "results": results,
    }
    output = Path(args.output) if args.output else Path(args.project_dir) / "query_layer_validation.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
