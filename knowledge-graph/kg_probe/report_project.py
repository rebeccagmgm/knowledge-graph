#!/usr/bin/env python3
"""Produce a compact quality report for a collected KG project snapshot."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator * 100.0 / denominator, 2)


def error_bucket(value: str) -> str:
    if not value:
        return ""
    if value.startswith("Error tokenizing"):
        return "sqlglot_tokenizing_error"
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="full")
    args = parser.parse_args()

    base = Path(args.project_dir)
    lineage = load(base / "lineage.json", {})
    details = load(base / "task_details.json", [])
    logs = load(base / "log_artifacts_full.json", load(base / "log_artifacts.json", []))
    statements = load(base / f"{args.prefix}_sql_statements.json", [])
    datasets = load(base / f"{args.prefix}_datasets.json", [])
    edges = load(base / f"{args.prefix}_dataset_edges.json", [])
    errors = load(base / f"{args.prefix}_sql_parse_errors.json", [])
    column_lineage = load(base / f"{args.prefix}_column_lineage.json", [])
    column_influence = load(base / f"{args.prefix}_column_influence.json", [])
    column_lineage_errors = load(base / f"{args.prefix}_column_lineage_errors.json", [])
    column_lineage_skipped = load(base / f"{args.prefix}_column_lineage_skipped.json", [])
    hard_parse_errors = [item for item in errors if item.get("status") != "regex_table_facts_extracted"]
    regex_fallbacks = [item for item in errors if item.get("status") == "regex_table_facts_extracted"]
    strategy_summary = load(base / f"{args.prefix}_sql_facts_summary.json", {})
    dms_records = load(base / "sz_metadata" / "dataset_dms.json", [])
    indicator_records = load(base / "sz_metadata" / "indicator_registry.json", [])
    sz_errors = load(base / "sz_metadata" / "sz_metadata_errors.json", [])
    graph_summary = load(base / f"{args.prefix}_graph_summary.json", {})
    query_validation = load(base / f"{args.prefix}_graph_query_validation.json", {})
    neo4j_validation = load(base / f"{args.prefix}_neo4j_validation.json", {})
    fact_audit = load(base / f"{args.prefix}_fact_audit.json", {})
    graph_node_count = graph_summary.get("node_count", 0)
    graph_edge_count = graph_summary.get("edge_count", 0)
    neo4j_node_count = neo4j_validation.get("validation", {}).get("node_count", 0)
    neo4j_edge_count = neo4j_validation.get("validation", {}).get("edge_count", 0)
    neo4j_matches_graph = bool(
        neo4j_node_count
        and neo4j_edge_count
        and neo4j_node_count == graph_node_count
        and neo4j_edge_count == graph_edge_count
    )
    graph_dataset_count = 0
    graph_dm_index_n_count = 0
    graph_nodes_path = base / f"{args.prefix}_graph_nodes.jsonl"
    if graph_nodes_path.exists():
        for line in graph_nodes_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if "Dataset" in item.get("labels", []):
                graph_dataset_count += 1
                if item.get("properties", {}).get("layer") == "dm_index_n":
                    graph_dm_index_n_count += 1
    metadata_dataset_target_count = graph_dataset_count or len(datasets)
    metadata_indicator_target_count = graph_dm_index_n_count or sum(
        1 for item in datasets if item.get("layer") == "dm_index_n"
    )

    task_count = lineage.get("summary", {}).get("node_count", 0)
    script_path_count = sum(bool(item.get("script_path")) for item in details)
    sync_info_count = sum(bool(item.get("sync_info")) for item in details)
    log_sql_count = sum(1 for item in logs if item.get("select_count", 0) or item.get("insert_count", 0))
    parse_ok_count = sum(1 for item in statements if item.get("parse_ok"))

    log_target_count = strategy_summary.get("log_task_count") or task_count
    log_coverage_pct = pct(len(logs), log_target_count)

    report = {
        "project_dir": str(base),
        "root_task_id": lineage.get("root_task_id"),
        "task_graph": lineage.get("summary", {}),
        "task_detail": {
            "collected": len(details),
            "coverage_pct": pct(len(details), task_count),
            "script_path_count": script_path_count,
            "script_path_pct": pct(script_path_count, len(details)),
            "sync_info_count": sync_info_count,
            "task_type_distribution": dict(Counter(item.get("task_type", "") for item in details)),
        },
        "runtime_logs": {
            "collected": len(logs),
            "expected_task_count": log_target_count,
            "coverage_pct": log_coverage_pct,
            "logs_with_sql_signal": log_sql_count,
            "logs_with_sql_signal_pct": pct(log_sql_count, len(logs)),
            "total_bytes": sum(item.get("bytes", 0) for item in logs),
        },
        "sql_facts": {
            "statement_count": len(statements),
            "parse_ok_count": parse_ok_count,
            "parse_ok_pct": pct(parse_ok_count, len(statements)),
            "dataset_count": len(datasets),
            "dataset_layer_distribution": dict(Counter(item.get("layer", "") for item in datasets)),
            "edge_count": len(edges),
            "edge_type_distribution": dict(Counter(item.get("relation", "") for item in edges)),
            "parse_error_count": len(hard_parse_errors),
            "regex_fallback_count": len(regex_fallbacks),
        },
        "sz_metadata": {
            "dms_collected": len(dms_records),
            "dms_target_count": metadata_dataset_target_count,
            "dms_coverage_pct": pct(len(dms_records), metadata_dataset_target_count),
            "dms_exact_count": sum(1 for item in dms_records if item.get("exact_count", 0) > 0),
            "dms_exact_pct": pct(sum(1 for item in dms_records if item.get("exact_count", 0) > 0), len(dms_records)),
            "indicator_collected": len(indicator_records),
            "indicator_target_count": metadata_indicator_target_count,
            "indicator_exact_count": sum(1 for item in indicator_records if item.get("exact_count", 0) > 0),
            "indicator_no_registry_count": sum(1 for item in indicator_records if item.get("status") == "no_registry"),
            "error_count": len(sz_errors),
        },
        "column_lineage": {
            "fact_count": len(column_lineage),
            "influence_fact_count": len(column_influence),
            "resolved_source_fact_count": sum(1 for item in column_lineage if item.get("source_dataset")),
            "resolved_source_fact_pct": pct(
                sum(1 for item in column_lineage if item.get("source_dataset")), len(column_lineage)
            ),
            "generated_column_fact_count": sum(1 for item in column_lineage if item.get("generation_type")),
            "generated_column_fact_pct": pct(
                sum(1 for item in column_lineage if item.get("generation_type")), len(column_lineage)
            ),
            "explainable_column_fact_count": sum(
                1 for item in column_lineage if item.get("source_dataset") or item.get("generation_type")
            ),
            "explainable_column_fact_pct": pct(
                sum(1 for item in column_lineage if item.get("source_dataset") or item.get("generation_type")),
                len(column_lineage),
            ),
            "target_dataset_count": len({item.get("target_dataset") for item in column_lineage if item.get("target_dataset")}),
            "source_dataset_count": len({item.get("source_dataset") for item in column_lineage if item.get("source_dataset")}),
            "error_count": len(column_lineage_errors),
            "skipped_count": len(column_lineage_skipped),
            "source_resolution_distribution": dict(
                Counter(item.get("source_resolution", "") for item in column_lineage)
            ),
            "influence_type_distribution": dict(
                Counter(item.get("influence_type", "") for item in column_influence)
            ),
            "influence_source_resolution_distribution": dict(
                Counter(item.get("source_resolution", "") for item in column_influence)
            ),
            "generation_type_distribution": dict(
                Counter(item.get("generation_type", "") for item in column_lineage if item.get("generation_type"))
            ),
            "target_resolution_distribution": dict(
                Counter(item.get("target_resolution", "") for item in column_lineage)
            ),
            "output_resolution_distribution": dict(
                Counter(item.get("output_resolution", "") for item in column_lineage if item.get("output_resolution"))
            ),
            "error_type_distribution": dict(
                Counter(error_bucket(item.get("error", "")) for item in column_lineage_errors)
            ),
            "skipped_reason_distribution": dict(Counter(item.get("reason", "") for item in column_lineage_skipped)),
        },
        "graph": {
            "node_count": graph_node_count,
            "edge_count": graph_edge_count,
            "node_label_distribution": graph_summary.get("node_label_distribution", {}),
            "edge_type_distribution": graph_summary.get("edge_type_distribution", {}),
        },
        "query_validation": {
            "missing_edge_endpoint_count": query_validation.get("endpoint_check", {}).get(
                "missing_edge_endpoint_count", 0
            ),
            "dataset_sample_count": query_validation.get("dataset_trace_check", {}).get("sample_dataset_count", 0),
            "datasets_with_source_path": query_validation.get("dataset_trace_check", {}).get(
                "datasets_with_source_path", 0
            ),
            "task_sample_count": query_validation.get("task_trace_check", {}).get("sample_task_count", 0),
            "tasks_with_terminal_path": query_validation.get("task_trace_check", {}).get(
                "tasks_with_terminal_path", 0
            ),
            "metric_sample_count": query_validation.get("metric_check", {}).get("sample_metric_count", 0),
            "metrics_with_storage": query_validation.get("metric_check", {}).get("metrics_with_storage", 0),
            "metrics_with_compute_task": query_validation.get("metric_check", {}).get(
                "metrics_with_compute_task", 0
            ),
        },
        "neo4j_validation": {
            "node_count": neo4j_node_count,
            "edge_count": neo4j_edge_count,
            "matches_current_graph": neo4j_matches_graph,
            "missing_edge_endpoint_count": neo4j_validation.get("validation", {}).get(
                "missing_edge_endpoint_count", 0
            ),
            "column_lineage_sample_count": neo4j_validation.get("validation", {}).get(
                "column_lineage_sample_count", 0
            ),
            "metric_check": neo4j_validation.get("validation", {}).get("metric_check", {}),
            "import": neo4j_validation.get("import", {}),
        },
        "fact_audit": {
            "missing_endpoint_count": fact_audit.get("missing_endpoint_count", 0),
            "missing_node_required_prop_count": fact_audit.get("missing_node_required_prop_count", 0),
            "missing_edge_required_prop_count": fact_audit.get("missing_edge_required_prop_count", 0),
            "isolated_node_count": fact_audit.get("isolated_node_count", 0),
            "metrics_without_compute_task_count": fact_audit.get("metrics_without_compute_task_count", 0),
            "datasets_without_columns_count": fact_audit.get("datasets_without_columns_count", 0),
            "low_confidence_derived_from_count": fact_audit.get("low_confidence_derived_from_count", 0),
            "node_confidence_distribution": fact_audit.get("node_confidence_distribution", {}),
            "edge_confidence_distribution": fact_audit.get("edge_confidence_distribution", {}),
        },
        "quality_flags": {
            "has_unexpanded_tasks": lineage.get("summary", {}).get("unexpanded_count", 0) > 0,
            "has_lineage_errors": lineage.get("summary", {}).get("error_count", 0) > 0,
            "has_low_log_coverage": log_coverage_pct < 80,
            "has_many_other_datasets": pct(
                sum(1 for item in datasets if item.get("layer") == "other"), len(datasets)
            )
            > 10,
            "has_sz_metadata_errors": len(sz_errors) > 0,
            "has_fact_integrity_errors": any(
                fact_audit.get(key, 0) > 0
                for key in [
                    "missing_endpoint_count",
                    "missing_node_required_prop_count",
                    "missing_edge_required_prop_count",
                    "isolated_node_count",
                ]
            ),
            "has_stale_neo4j_validation": bool(neo4j_validation) and not neo4j_matches_graph,
        },
    }
    out = base / f"{args.prefix}_quality_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
