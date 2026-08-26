#!/usr/bin/env python3
"""Build table-level semantic profiles for ontology_v2."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from ontology_v2_utils import (
    clean_text,
    compact_counter,
    graph_paths,
    infer_concepts,
    infer_table_role,
    labels_of,
    load_graph,
    node_quality,
    now_iso,
    props_of,
    stable_hash,
    tokenize,
    write_json,
    write_jsonl,
)


def build_profiles(project_dir: Path, prefix: str, project_key_override: str | None = None) -> tuple[list[dict], dict]:
    nodes, edges, meta = load_graph(project_dir, prefix)
    datasets: dict[str, dict] = {}
    columns_by_dataset: dict[str, list[dict]] = defaultdict(list)

    for node_id, node in nodes.items():
        props = props_of(node)
        if "Dataset" in labels_of(node):
            name = clean_text(props.get("name") or node_id.removeprefix("dataset:")).lower()
            datasets[node_id] = {"id": node_id, "name": name, "properties": props, "columns": []}
        elif "Column" in labels_of(node):
            dataset = clean_text(props.get("dataset")).lower()
            if dataset:
                columns_by_dataset[dataset].append(
                    {
                        "id": node_id,
                        "name": clean_text(props.get("name") or node_id.rsplit(".", 1)[-1]),
                        "comment": clean_text(props.get("comment")),
                        "quality_score": node_quality(props),
                    }
                )

    dataset_id_by_name = {item["name"]: item_id for item_id, item in datasets.items()}
    upstream_by_dataset: dict[str, set[str]] = defaultdict(set)
    downstream_by_dataset: dict[str, set[str]] = defaultdict(set)
    producers_by_dataset: dict[str, set[str]] = defaultdict(set)
    consumers_by_dataset: dict[str, set[str]] = defaultdict(set)
    sql_reads_by_dataset: dict[str, set[str]] = defaultdict(set)
    evidence_by_dataset: dict[str, set[str]] = defaultdict(set)

    for edge in edges:
        rel_type = edge.get("type")
        from_id = edge.get("from")
        to_id = edge.get("to")
        props = props_of(edge)
        edge_id = edge.get("id")
        if rel_type == "DATASET_DEPENDS_ON" and from_id in datasets and to_id in datasets:
            upstream_by_dataset[from_id].add(to_id)
            downstream_by_dataset[to_id].add(from_id)
            evidence_by_dataset[from_id].add(edge_id)
            evidence_by_dataset[to_id].add(edge_id)
        elif rel_type == "PRODUCES" and to_id in datasets:
            producers_by_dataset[to_id].add(from_id)
            evidence_by_dataset[to_id].add(edge_id)
        elif rel_type == "CONSUMES" and to_id in datasets:
            consumers_by_dataset[to_id].add(from_id)
            evidence_by_dataset[to_id].add(edge_id)
        elif rel_type == "READS" and to_id in datasets:
            task_id = clean_text(props.get("task_id"))
            if task_id:
                sql_reads_by_dataset[to_id].add(f"task:{task_id}")
            evidence_by_dataset[to_id].add(edge_id)

    built_at = now_iso()
    profiles: list[dict] = []
    for dataset_id, item in sorted(datasets.items()):
        props = item["properties"]
        name = item["name"]
        project_key = project_key_override or props.get("project_key") or project_dir.name
        layer = clean_text(props.get("layer"))
        comment = clean_text(props.get("comment"))
        columns = sorted(columns_by_dataset.get(name, []), key=lambda col: col["id"])
        column_names = [col["name"] for col in columns]
        column_comments = [col["comment"] for col in columns]
        upstream = sorted(upstream_by_dataset.get(dataset_id, set()))
        downstream = sorted(downstream_by_dataset.get(dataset_id, set()))
        producers = sorted(producers_by_dataset.get(dataset_id, set()))
        consumers = sorted(consumers_by_dataset.get(dataset_id, set()) | sql_reads_by_dataset.get(dataset_id, set()))
        text = " ".join([name, comment, " ".join(column_names[:120]), " ".join(column_comments[:120])])
        concepts = infer_concepts(text, limit=8)
        table_role = infer_table_role(name, comment, layer, column_names, len(upstream), len(downstream))
        subject_tokens = [token for token in tokenize(" ".join([name, comment])) if token not in {"table", "data"}]
        business_subject = comment or " / ".join(subject_tokens[:6]) or name
        top_column_concepts = compact_counter(
            concept
            for col in columns
            for concept in infer_concepts(" ".join([col["name"], col["comment"]]), limit=3)
        )
        profile_id = "table_profile:" + stable_hash([project_key, dataset_id])
        profiles.append(
            {
                "id": profile_id,
                "project_key": project_key,
                "dataset_id": dataset_id,
                "dataset": name,
                "layer": layer,
                "table_role": table_role,
                "business_subject": business_subject,
                "semantic_concepts": concepts,
                "column_count": len(columns),
                "top_column_concepts": top_column_concepts,
                "sample_columns": columns[:80],
                "upstream_datasets": upstream[:80],
                "downstream_datasets": downstream[:80],
                "producer_tasks": producers[:40],
                "consumer_tasks": consumers[:80],
                "quality_score": node_quality(props),
                "confidence": "medium" if concepts or comment else "low",
                "knowledge_admission": "needs_review",
                "quality_tier": "ontology_candidate",
                "evidence_ids": sorted(evidence_by_dataset.get(dataset_id, set()))[:120],
                "built_at": built_at,
                "method": "table_profile_rules.v1",
            }
        )

    summary = {
        "project_key": project_key_override or (profiles[0]["project_key"] if profiles else project_dir.name),
        "generated_at": built_at,
        "profile_count": len(profiles),
        "table_role_distribution": dict(Counter(item["table_role"] for item in profiles)),
        "semantic_concept_distribution": dict(Counter(concept for item in profiles for concept in item["semantic_concepts"])),
        "artifact_meta": meta,
    }
    return profiles, summary


def graph_nodes(profiles: list[dict]) -> list[dict]:
    return [
        {
            "id": item["id"],
            "labels": ["TableProfile", "OntologyCandidate"],
            "properties": {
                "project_key": item["project_key"],
                "dataset": item["dataset"],
                "dataset_id": item["dataset_id"],
                "table_role": item["table_role"],
                "business_subject": item["business_subject"],
                "semantic_concepts": json.dumps(item["semantic_concepts"], ensure_ascii=False),
                "column_count": item["column_count"],
                "confidence": item["confidence"],
                "quality_score": item["quality_score"],
                "knowledge_admission": item["knowledge_admission"],
                "quality_tier": item["quality_tier"],
                "fact_type": "ontology_table_profile",
                "inferred": True,
                "built_at": item["built_at"],
            },
        }
        for item in profiles
    ]


def graph_edges(profiles: list[dict]) -> list[dict]:
    rows = []
    for item in profiles:
        rows.append(
            {
                "id": f"{item['dataset_id']}->HAS_TABLE_PROFILE->{item['id']}",
                "from": item["dataset_id"],
                "to": item["id"],
                "type": "HAS_TABLE_PROFILE",
                "properties": {
                    "project_key": item["project_key"],
                    "fact_type": "ontology_table_profile",
                    "confidence": item["confidence"],
                    "knowledge_admission": item["knowledge_admission"],
                    "quality_tier": item["quality_tier"],
                    "source_type": item["method"],
                    "inferred": True,
                    "built_at": item["built_at"],
                },
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--project-key", default=None)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    out_dir = Path(args.output_dir) if args.output_dir else project_dir / "ontology_v2"
    profiles, summary = build_profiles(project_dir, args.prefix, args.project_key)
    write_jsonl(out_dir / "table_profiles.jsonl", profiles)
    write_json(out_dir / "table_profiles_summary.json", summary)
    write_jsonl(out_dir / "table_profile_graph_nodes.jsonl", graph_nodes(profiles))
    write_jsonl(out_dir / "table_profile_graph_edges.jsonl", graph_edges(profiles))
    print(json.dumps({"output_dir": str(out_dir), **summary}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
