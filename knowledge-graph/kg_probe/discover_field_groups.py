#!/usr/bin/env python3
"""Discover table-local semantic field groups for ontology_v2."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from ontology_v2_utils import (
    clean_text,
    graph_paths,
    group_by_concept,
    infer_concepts,
    labels_of,
    load_graph,
    load_jsonl,
    node_quality,
    now_iso,
    props_of,
    stable_hash,
    tokenize,
    write_json,
    write_jsonl,
)


CONCEPT_NAMES = {
    "agreement": "合约/协议",
    "counterparty_customer": "交易对手/客户",
    "organization_role": "人员/机构/归属",
    "product_underlying": "产品/标的",
    "sales_revenue": "销售收入/创收",
    "rate_fee": "费率/费用/收益率",
    "principal_margin": "本金/保证金",
    "risk_qualification": "风险/资质/授信",
    "reference_config": "参数/配置/映射",
    "time_lifecycle": "时间/生命周期",
}


GROUP_TYPES = {
    "agreement": "business_object",
    "counterparty_customer": "business_object",
    "organization_role": "business_role",
    "product_underlying": "business_object",
    "sales_revenue": "business_fact",
    "rate_fee": "business_measure_property",
    "principal_margin": "business_measure_property",
    "risk_qualification": "control_attribute",
    "reference_config": "reference_attribute",
    "time_lifecycle": "lifecycle_attribute",
}


def load_table_profiles(out_dir: Path) -> dict[str, dict]:
    return {item["dataset_id"]: item for item in load_jsonl(out_dir / "table_profiles.jsonl")}


def build_field_groups(project_dir: Path, prefix: str, out_dir: Path, project_key_override: str | None = None) -> tuple[list[dict], dict]:
    nodes, edges, meta = load_graph(project_dir, prefix)
    table_profiles = load_table_profiles(out_dir)
    columns_by_dataset_id: dict[str, list[dict]] = defaultdict(list)
    dataset_id_by_name: dict[str, str] = {}

    for node_id, node in nodes.items():
        props = props_of(node)
        if "Dataset" in labels_of(node):
            dataset_id_by_name[clean_text(props.get("name") or node_id.removeprefix("dataset:")).lower()] = node_id

    column_evidence: dict[str, set[str]] = defaultdict(set)
    lineage_touch_count: Counter[str] = Counter()
    for edge in edges:
        rel_type = edge.get("type")
        from_id = edge.get("from")
        to_id = edge.get("to")
        edge_id = edge.get("id")
        if rel_type in {"DERIVED_FROM", "INFLUENCED_BY"}:
            if from_id:
                column_evidence[from_id].add(edge_id)
                lineage_touch_count[from_id] += 1
            if to_id:
                column_evidence[to_id].add(edge_id)
                lineage_touch_count[to_id] += 1
        elif rel_type == "HAS_COLUMN" and to_id:
            column_evidence[to_id].add(edge_id)

    for node_id, node in nodes.items():
        labels = labels_of(node)
        props = props_of(node)
        if "Column" not in labels:
            continue
        dataset = clean_text(props.get("dataset")).lower()
        dataset_id = dataset_id_by_name.get(dataset)
        if not dataset_id:
            continue
        columns_by_dataset_id[dataset_id].append(
            {
                "id": node_id,
                "name": clean_text(props.get("name") or node_id.rsplit(".", 1)[-1]),
                "comment": clean_text(props.get("comment")),
                "dataset": dataset,
                "quality_score": node_quality(props),
                "lineage_touch_count": lineage_touch_count[node_id],
                "evidence_ids": sorted(column_evidence.get(node_id, set()))[:40],
            }
        )

    built_at = now_iso()
    groups: list[dict] = []
    for dataset_id, columns in sorted(columns_by_dataset_id.items()):
        table_profile = table_profiles.get(dataset_id, {})
        if len(columns) < 2:
            continue
        deduped_columns = {}
        for column in columns:
            key = clean_text(column.get("name")).lower()
            current = deduped_columns.get(key)
            if not current or (column.get("comment") and not current.get("comment")) or column.get("quality_score", 0) > current.get("quality_score", 0):
                deduped_columns[key] = column
        columns = list(deduped_columns.values())
        local_groups = group_by_concept(columns)
        for concept, members in sorted(local_groups.items()):
            if len(members) < 2:
                continue
            if concept.startswith("other_") and len(members) < 4:
                continue
            table_concepts = set(table_profile.get("semantic_concepts") or [])
            concept_text = " ".join(
                [concept, table_profile.get("business_subject") or "", table_profile.get("table_role") or ""]
                + [member["name"] + " " + member.get("comment", "") for member in members[:80]]
            )
            group_concepts = infer_concepts(concept_text, limit=5)
            display_name = CONCEPT_NAMES.get(concept) or CONCEPT_NAMES.get(group_concepts[0], "") if group_concepts else ""
            if not display_name:
                tokens = [token for token, _ in Counter(token for member in members for token in tokenize(member["name"] + " " + member.get("comment", ""))).most_common(5)]
                display_name = " / ".join(tokens) or concept
            evidence_ids = sorted({evidence for member in members for evidence in member.get("evidence_ids", [])})[:120]
            lineage_supported = sum(1 for member in members if member.get("lineage_touch_count", 0) > 0)
            confidence_score = 0.35
            if concept in table_concepts or set(group_concepts) & table_concepts:
                confidence_score += 0.2
            if lineage_supported:
                confidence_score += min(0.25, lineage_supported / max(len(members), 1) * 0.25)
            if any(member.get("comment") for member in members):
                confidence_score += 0.1
            if len(members) >= 4:
                confidence_score += 0.05
            confidence = "high" if confidence_score >= 0.75 else ("medium" if confidence_score >= 0.5 else "low")
            group_id = "field_group:" + stable_hash([dataset_id, concept, [member["id"] for member in members]])
            groups.append(
                {
                    "id": group_id,
                    "project_key": project_key_override or table_profile.get("project_key") or project_dir.name,
                    "dataset_id": dataset_id,
                    "dataset": members[0]["dataset"],
                    "table_profile_id": table_profile.get("id"),
                    "group_name": display_name,
                    "group_key": concept,
                    "group_type": GROUP_TYPES.get(concept, "semantic_attribute_group"),
                    "semantic_concepts": group_concepts,
                    "field_count": len(members),
                    "fields": members[:120],
                    "lineage_supported_field_count": lineage_supported,
                    "summary": f"{display_name}字段组，位于{members[0]['dataset']}，包含{len(members)}个字段。",
                    "confidence": confidence,
                    "evidence_score": round(confidence_score, 4),
                    "knowledge_admission": "needs_review",
                    "quality_tier": "ontology_candidate",
                    "evidence_ids": evidence_ids,
                    "built_at": built_at,
                    "method": "field_group_rules.v1",
                }
            )

    summary = {
        "project_key": project_key_override or (groups[0]["project_key"] if groups else project_dir.name),
        "generated_at": built_at,
        "field_group_count": len(groups),
        "group_type_distribution": dict(Counter(item["group_type"] for item in groups)),
        "group_key_distribution": dict(Counter(item["group_key"] for item in groups)),
        "confidence_distribution": dict(Counter(item["confidence"] for item in groups)),
        "artifact_meta": meta,
    }
    return groups, summary


def graph_nodes(groups: list[dict]) -> list[dict]:
    rows = []
    for item in groups:
        rows.append(
            {
                "id": item["id"],
                "labels": ["SemanticFieldGroup", "OntologyCandidate"],
                "properties": {
                    "project_key": item["project_key"],
                    "dataset": item["dataset"],
                    "dataset_id": item["dataset_id"],
                    "group_name": item["group_name"],
                    "group_key": item["group_key"],
                    "group_type": item["group_type"],
                    "semantic_concepts": json.dumps(item["semantic_concepts"], ensure_ascii=False),
                    "field_count": item["field_count"],
                    "lineage_supported_field_count": item["lineage_supported_field_count"],
                    "summary": item["summary"],
                    "confidence": item["confidence"],
                    "evidence_score": item["evidence_score"],
                    "knowledge_admission": item["knowledge_admission"],
                    "quality_tier": item["quality_tier"],
                    "fact_type": "ontology_field_group",
                    "inferred": True,
                    "built_at": item["built_at"],
                },
            }
        )
    return rows


def graph_edges(groups: list[dict]) -> list[dict]:
    rows = []
    for item in groups:
        if item.get("table_profile_id"):
            rows.append(
                {
                    "id": f"{item['table_profile_id']}->HAS_FIELD_GROUP->{item['id']}",
                    "from": item["table_profile_id"],
                    "to": item["id"],
                    "type": "HAS_FIELD_GROUP",
                    "properties": {
                        "project_key": item["project_key"],
                        "fact_type": "ontology_field_group",
                        "confidence": item["confidence"],
                        "knowledge_admission": item["knowledge_admission"],
                        "quality_tier": item["quality_tier"],
                        "source_type": item["method"],
                        "inferred": True,
                        "built_at": item["built_at"],
                    },
                }
            )
        for field in item["fields"]:
            rows.append(
                {
                    "id": f"{item['id']}->CONTAINS_COLUMN->{field['id']}",
                    "from": item["id"],
                    "to": field["id"],
                    "type": "CONTAINS_COLUMN",
                    "properties": {
                        "project_key": item["project_key"],
                        "fact_type": "ontology_field_group",
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
    if not (out_dir / "table_profiles.jsonl").exists():
        raise SystemExit(f"Missing table profiles: {out_dir / 'table_profiles.jsonl'}")
    groups, summary = build_field_groups(project_dir, args.prefix, out_dir, args.project_key)
    write_jsonl(out_dir / "field_groups.jsonl", groups)
    write_json(out_dir / "field_groups_summary.json", summary)
    write_jsonl(out_dir / "field_group_graph_nodes.jsonl", graph_nodes(groups))
    write_jsonl(out_dir / "field_group_graph_edges.jsonl", graph_edges(groups))
    print(json.dumps({"output_dir": str(out_dir), **summary}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
