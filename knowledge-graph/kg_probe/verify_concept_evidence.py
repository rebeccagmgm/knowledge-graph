#!/usr/bin/env python3
"""Verify ontology_v2 field groups with lineage and graph evidence."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from ontology_v2_utils import clean_text, labels_of, load_graph, load_jsonl, now_iso, props_of, stable_hash, write_json, write_jsonl


def verify_groups(project_dir: Path, prefix: str, out_dir: Path, project_key_override: str | None = None) -> tuple[list[dict], dict]:
    nodes, edges, meta = load_graph(project_dir, prefix)
    groups = list(load_jsonl(out_dir / "field_groups.jsonl"))
    column_to_group: dict[str, set[str]] = defaultdict(set)
    group_by_id = {item["id"]: item for item in groups}
    for group in groups:
        for field in group.get("fields") or []:
            column_to_group[field["id"]].add(group["id"])

    dataset_upstream: dict[str, set[str]] = defaultdict(set)
    dataset_downstream: dict[str, set[str]] = defaultdict(set)
    field_lineage_in: dict[str, list[dict]] = defaultdict(list)
    field_lineage_out: dict[str, list[dict]] = defaultdict(list)
    group_relations: dict[tuple[str, str], list[dict]] = defaultdict(list)
    evidence_ids_by_group: dict[str, set[str]] = defaultdict(set)

    for edge in edges:
        rel_type = edge.get("type")
        from_id = edge.get("from")
        to_id = edge.get("to")
        props = props_of(edge)
        edge_id = edge.get("id")
        if rel_type == "DATASET_DEPENDS_ON":
            dataset_upstream[from_id].add(to_id)
            dataset_downstream[to_id].add(from_id)
        elif rel_type in {"DERIVED_FROM", "INFLUENCED_BY"} and from_id and to_id:
            fact = {
                "edge_id": edge_id,
                "relation": rel_type,
                "from": from_id,
                "to": to_id,
                "task_id": clean_text(props.get("task_id")),
                "statement_id": clean_text(props.get("statement_id")),
                "source_resolution": clean_text(props.get("source_resolution")),
                "target_resolution": clean_text(props.get("target_resolution")),
                "confidence": clean_text(props.get("confidence")),
            }
            field_lineage_in[from_id].append(fact)
            field_lineage_out[to_id].append(fact)
            for left_group in column_to_group.get(from_id, set()):
                evidence_ids_by_group[left_group].add(edge_id)
                for right_group in column_to_group.get(to_id, set()):
                    evidence_ids_by_group[right_group].add(edge_id)
                    if left_group != right_group:
                        group_relations[(left_group, right_group)].append(fact)

    verified: list[dict] = []
    built_at = now_iso()
    for group in groups:
        field_ids = [field["id"] for field in group.get("fields") or []]
        incoming = [fact for field_id in field_ids for fact in field_lineage_in.get(field_id, [])]
        outgoing = [fact for field_id in field_ids for fact in field_lineage_out.get(field_id, [])]
        datasets = {field.get("dataset") for field in group.get("fields") or [] if field.get("dataset")}
        dataset_ids = {f"dataset:{dataset}" for dataset in datasets}
        table_upstream = sorted({src for dataset_id in dataset_ids for src in dataset_upstream.get(dataset_id, set())})
        table_downstream = sorted({dst for dataset_id in dataset_ids for dst in dataset_downstream.get(dataset_id, set())})
        direct_field_count = len({fact["from"] for fact in incoming} | {fact["to"] for fact in outgoing})
        evidence_score = 0.2
        if incoming:
            evidence_score += 0.3
        if outgoing:
            evidence_score += 0.2
        if table_upstream:
            evidence_score += 0.12
        if table_downstream:
            evidence_score += 0.08
        if group.get("evidence_score"):
            evidence_score += min(0.15, float(group["evidence_score"]) * 0.15)
        evidence_score = min(1.0, evidence_score)
        if evidence_score >= 0.72:
            evidence_level = "strong"
        elif evidence_score >= 0.48:
            evidence_level = "medium"
        else:
            evidence_level = "weak"
        verified_group = dict(group)
        verified_group.update(
            {
                "verification_id": "evidence_verification:" + stable_hash([group["id"], incoming[:20], outgoing[:20]]),
                "evidence_level": evidence_level,
                "verified_evidence_score": round(evidence_score, 4),
                "incoming_field_lineage_count": len(incoming),
                "outgoing_field_lineage_count": len(outgoing),
                "lineage_touched_field_count": direct_field_count,
                "upstream_datasets": table_upstream[:80],
                "downstream_datasets": table_downstream[:80],
                "sample_incoming_lineage": incoming[:40],
                "sample_outgoing_lineage": outgoing[:40],
                "verification_method": "lineage_evidence_rules.v1",
                "verified_at": built_at,
                "evidence_ids": sorted(set(group.get("evidence_ids") or []) | evidence_ids_by_group.get(group["id"], set()))[:160],
            }
        )
        verified.append(verified_group)

    relation_rows = []
    for (left_group, right_group), facts in group_relations.items():
        left = group_by_id.get(left_group, {})
        right = group_by_id.get(right_group, {})
        relation_rows.append(
            {
                "id": "field_group_relation:" + stable_hash([left_group, right_group, facts[:20]]),
                "project_key": project_key_override or left.get("project_key") or right.get("project_key") or project_dir.name,
                "from_group_id": left_group,
                "to_group_id": right_group,
                "relationship_type": "DERIVED_FROM_GROUP",
                "field_lineage_count": len(facts),
                "shared_task_ids": sorted({fact["task_id"] for fact in facts if fact.get("task_id")})[:40],
                "sample_lineage": facts[:40],
                "confidence": "high" if len(facts) >= 3 else "medium",
                "evidence_score": min(1.0, 0.55 + min(len(facts), 10) * 0.04),
                "knowledge_admission": "needs_review",
                "quality_tier": "ontology_candidate",
                "built_at": built_at,
                "method": "lineage_evidence_rules.v1",
            }
        )

    summary = {
        "project_key": project_key_override or (verified[0]["project_key"] if verified else project_dir.name),
        "generated_at": built_at,
        "verified_group_count": len(verified),
        "evidence_level_distribution": dict(Counter(item["evidence_level"] for item in verified)),
        "group_relation_count": len(relation_rows),
        "artifact_meta": meta,
    }
    return verified, relation_rows, summary


def graph_nodes(groups: list[dict]) -> list[dict]:
    rows = []
    for item in groups:
        rows.append(
            {
                "id": item["verification_id"],
                "labels": ["OntologyEvidence", "OntologyCandidate"],
                "properties": {
                    "project_key": item["project_key"],
                    "field_group_id": item["id"],
                    "evidence_level": item["evidence_level"],
                    "evidence_score": item["verified_evidence_score"],
                    "incoming_field_lineage_count": item["incoming_field_lineage_count"],
                    "outgoing_field_lineage_count": item["outgoing_field_lineage_count"],
                    "knowledge_admission": "needs_review",
                    "quality_tier": "ontology_candidate",
                    "fact_type": "ontology_evidence",
                    "confidence": item["evidence_level"],
                    "inferred": True,
                    "built_at": item["verified_at"],
                },
            }
        )
    return rows


def graph_edges(groups: list[dict], relations: list[dict]) -> list[dict]:
    rows = []
    for item in groups:
        rows.append(
            {
                "id": f"{item['id']}->SUPPORTED_BY->{item['verification_id']}",
                "from": item["id"],
                "to": item["verification_id"],
                "type": "SUPPORTED_BY",
                "properties": {
                    "project_key": item["project_key"],
                    "fact_type": "ontology_evidence",
                    "evidence_level": item["evidence_level"],
                    "evidence_score": item["verified_evidence_score"],
                    "confidence": item["evidence_level"],
                    "knowledge_admission": "needs_review",
                    "quality_tier": "ontology_candidate",
                    "source_type": item["verification_method"],
                    "inferred": True,
                    "built_at": item["verified_at"],
                },
            }
        )
    for item in relations:
        rows.append(
            {
                "id": f"{item['from_group_id']}->DERIVED_FROM_GROUP->{item['to_group_id']}:{stable_hash(item['id'], 8)}",
                "from": item["from_group_id"],
                "to": item["to_group_id"],
                "type": "DERIVED_FROM_GROUP",
                "properties": {
                    "project_key": item["project_key"],
                    "fact_type": "ontology_group_relation",
                    "field_lineage_count": item["field_lineage_count"],
                    "evidence_score": item["evidence_score"],
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
    if not (out_dir / "field_groups.jsonl").exists():
        raise SystemExit(f"Missing field groups: {out_dir / 'field_groups.jsonl'}")
    groups, relations, summary = verify_groups(project_dir, args.prefix, out_dir, args.project_key)
    write_jsonl(out_dir / "verified_field_groups.jsonl", groups)
    write_jsonl(out_dir / "field_group_relations.jsonl", relations)
    write_json(out_dir / "evidence_verification_summary.json", summary)
    write_jsonl(out_dir / "evidence_graph_nodes.jsonl", graph_nodes(groups))
    write_jsonl(out_dir / "evidence_graph_edges.jsonl", graph_edges(groups, relations))
    print(json.dumps({"output_dir": str(out_dir), **summary}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
