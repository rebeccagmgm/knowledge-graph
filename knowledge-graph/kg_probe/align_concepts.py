#!/usr/bin/env python3
"""Align ontology_v2 semantic field groups into cross-table concept candidates."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from ontology_v2_utils import clean_text, jaccard, load_jsonl, now_iso, stable_hash, tokenize, write_json, write_jsonl


def group_tokens(group: dict) -> set[str]:
    text = " ".join(
        [
            group.get("group_name") or "",
            group.get("group_key") or "",
            group.get("summary") or "",
            " ".join(group.get("semantic_concepts") or []),
            " ".join(field.get("name", "") + " " + field.get("comment", "") for field in group.get("fields", [])[:80]),
        ]
    )
    return set(tokenize(text))


def table_context_tokens(table_profile: dict | None) -> set[str]:
    if not table_profile:
        return set()
    text = " ".join(
        [
            table_profile.get("table_role") or "",
            table_profile.get("business_subject") or "",
            " ".join(table_profile.get("semantic_concepts") or []),
        ]
    )
    return set(tokenize(text))


def relation_type_for(left: dict, right: dict, score: float, evidence: dict) -> str:
    if evidence["group_lineage_bridge"]:
        return "derived_variant"
    if left.get("group_key") == right.get("group_key") and evidence["upstream_overlap"] >= 0.15:
        return "same_source_variant"
    if left.get("group_key") == right.get("group_key") and evidence["field_token_similarity"] >= 0.35:
        return "same_concept_candidate"
    if score >= 0.72:
        return "same_concept_candidate"
    return "weak_related"


def align(project_dir: Path, out_dir: Path, project_key_override: str | None = None, max_pairs: int = 12000) -> tuple[list[dict], list[dict], dict]:
    groups = list(load_jsonl(out_dir / "verified_field_groups.jsonl"))
    table_profiles = {item["id"]: item for item in load_jsonl(out_dir / "table_profiles.jsonl")}
    table_by_dataset = {item["dataset_id"]: item for item in table_profiles.values()}
    group_relations = list(load_jsonl(out_dir / "field_group_relations.jsonl"))
    lineage_pairs = {(item["from_group_id"], item["to_group_id"]) for item in group_relations}

    buckets: dict[str, list[dict]] = defaultdict(list)
    for group in groups:
        key = group.get("group_key") or (group.get("semantic_concepts") or ["unknown"])[0]
        if key.startswith("other_") and group.get("evidence_level") != "strong":
            continue
        buckets[key].append(group)

    relations: dict[str, dict] = {}
    for members in buckets.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda item: (-(item.get("verified_evidence_score", 0)), item["id"]))[:180]
        for idx, left in enumerate(members):
            for right in members[idx + 1 :]:
                if left.get("dataset_id") == right.get("dataset_id"):
                    continue
                left_tokens = group_tokens(left)
                right_tokens = group_tokens(right)
                left_table = table_by_dataset.get(left.get("dataset_id"))
                right_table = table_by_dataset.get(right.get("dataset_id"))
                table_sim = jaccard(table_context_tokens(left_table), table_context_tokens(right_table))
                field_sim = jaccard(left_tokens, right_tokens)
                upstream_sim = jaccard(set(left.get("upstream_datasets") or []), set(right.get("upstream_datasets") or []))
                downstream_sim = jaccard(set(left.get("downstream_datasets") or []), set(right.get("downstream_datasets") or []))
                group_lineage_bridge = (left["id"], right["id"]) in lineage_pairs or (right["id"], left["id"]) in lineage_pairs
                same_group_key = 1.0 if left.get("group_key") and left.get("group_key") == right.get("group_key") else 0.0
                evidence_score = (left.get("verified_evidence_score", 0) + right.get("verified_evidence_score", 0)) / 2
                score = (
                    same_group_key * 0.22
                    + field_sim * 0.24
                    + table_sim * 0.14
                    + upstream_sim * 0.18
                    + downstream_sim * 0.08
                    + evidence_score * 0.14
                )
                if group_lineage_bridge:
                    score = max(score, 0.68)
                if str(left.get("group_key", "")).startswith("other_") or str(right.get("group_key", "")).startswith("other_"):
                    if not group_lineage_bridge or min(left.get("verified_evidence_score", 0), right.get("verified_evidence_score", 0)) < 0.72:
                        continue
                if score < 0.46:
                    continue
                evidence = {
                    "field_token_similarity": round(field_sim, 4),
                    "table_context_similarity": round(table_sim, 4),
                    "upstream_overlap": round(upstream_sim, 4),
                    "downstream_overlap": round(downstream_sim, 4),
                    "group_lineage_bridge": group_lineage_bridge,
                    "same_group_key": bool(same_group_key),
                    "shared_tokens": sorted(left_tokens & right_tokens)[:30],
                    "shared_upstream_datasets": sorted(set(left.get("upstream_datasets") or []) & set(right.get("upstream_datasets") or []))[:30],
                    "shared_downstream_datasets": sorted(set(left.get("downstream_datasets") or []) & set(right.get("downstream_datasets") or []))[:30],
                }
                rel_type = relation_type_for(left, right, score, evidence)
                rel_id = "concept_alignment:" + stable_hash([left["id"], right["id"], evidence])
                relations[rel_id] = {
                    "id": rel_id,
                    "project_key": project_key_override or left.get("project_key") or right.get("project_key") or project_dir.name,
                    "from_group_id": left["id"],
                    "to_group_id": right["id"],
                    "from_group_name": left.get("group_name"),
                    "to_group_name": right.get("group_name"),
                    "from_dataset": left.get("dataset"),
                    "to_dataset": right.get("dataset"),
                    "relationship_type": rel_type,
                    "score": round(score, 4),
                    "confidence": "high" if score >= 0.72 else ("medium" if score >= 0.58 else "low"),
                    "evidence": evidence,
                    "knowledge_admission": "needs_review",
                    "quality_tier": "ontology_candidate",
                    "built_at": now_iso(),
                    "method": "concept_alignment_rules.v1",
                }
                if len(relations) >= max_pairs:
                    break
            if len(relations) >= max_pairs:
                break
        if len(relations) >= max_pairs:
            break

    # Build concept candidates by connected components over non-weak relations.
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        if parent[item] != item:
            parent[item] = find(parent[item])
        return parent[item]

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for rel in relations.values():
        if rel["relationship_type"] != "weak_related" and rel["score"] >= 0.58:
            union(rel["from_group_id"], rel["to_group_id"])

    grouped_ids: dict[str, set[str]] = defaultdict(set)
    for rel in relations.values():
        for group_id in [rel["from_group_id"], rel["to_group_id"]]:
            if group_id in parent:
                grouped_ids[find(group_id)].add(group_id)

    group_by_id = {item["id"]: item for item in groups}
    concepts: list[dict] = []
    built_at = now_iso()
    for ids in grouped_ids.values():
        if len(ids) < 2:
            continue
        members = [group_by_id[group_id] for group_id in sorted(ids) if group_id in group_by_id]
        name_counts = Counter(member.get("group_name") for member in members if member.get("group_name"))
        key_counts = Counter(member.get("group_key") for member in members if member.get("group_key"))
        concept_name = name_counts.most_common(1)[0][0] if name_counts else (key_counts.most_common(1)[0][0] if key_counts else "未命名概念")
        rels = [rel for rel in relations.values() if rel["from_group_id"] in ids and rel["to_group_id"] in ids]
        scores = [rel["score"] for rel in rels]
        concept_id = "concept_candidate:" + stable_hash(sorted(ids))
        concepts.append(
            {
                "id": concept_id,
                "project_key": project_key_override or (members[0].get("project_key") if members else project_dir.name),
                "concept_name": concept_name,
                "concept_key": key_counts.most_common(1)[0][0] if key_counts else "",
                "member_count": len(members),
                "members": [
                    {
                        "field_group_id": member["id"],
                        "group_name": member.get("group_name"),
                        "dataset": member.get("dataset"),
                        "group_type": member.get("group_type"),
                        "field_count": member.get("field_count"),
                        "evidence_level": member.get("evidence_level"),
                        "verified_evidence_score": member.get("verified_evidence_score"),
                    }
                    for member in members[:80]
                ],
                "relationship_count": len(rels),
                "relationship_types": dict(Counter(rel["relationship_type"] for rel in rels)),
                "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
                "max_score": round(max(scores), 4) if scores else 0.0,
                "confidence": "high" if scores and max(scores) >= 0.72 else "medium",
                "knowledge_admission": "needs_review",
                "quality_tier": "ontology_candidate",
                "built_at": built_at,
                "method": "concept_alignment_rules.v1",
            }
        )

    relations_list = sorted(relations.values(), key=lambda item: (-item["score"], item["relationship_type"], item["id"]))
    concepts = sorted(concepts, key=lambda item: (-item["max_score"], -item["member_count"], item["id"]))
    summary = {
        "project_key": project_key_override or (groups[0]["project_key"] if groups else project_dir.name),
        "generated_at": built_at,
        "concept_candidate_count": len(concepts),
        "alignment_relation_count": len(relations_list),
        "alignment_relation_distribution": dict(Counter(item["relationship_type"] for item in relations_list)),
        "confidence_distribution": dict(Counter(item["confidence"] for item in relations_list)),
    }
    return concepts, relations_list, summary


def graph_nodes(concepts: list[dict]) -> list[dict]:
    return [
        {
            "id": item["id"],
            "labels": ["ConceptCandidate", "OntologyCandidate"],
            "properties": {
                "project_key": item["project_key"],
                "concept_name": item["concept_name"],
                "concept_key": item["concept_key"],
                "member_count": item["member_count"],
                "relationship_count": item["relationship_count"],
                "relationship_types": json.dumps(item["relationship_types"], ensure_ascii=False, sort_keys=True),
                "avg_score": item["avg_score"],
                "max_score": item["max_score"],
                "confidence": item["confidence"],
                "knowledge_admission": item["knowledge_admission"],
                "quality_tier": item["quality_tier"],
                "fact_type": "ontology_concept_candidate",
                "inferred": True,
                "built_at": item["built_at"],
            },
        }
        for item in concepts
    ]


def graph_edges(concepts: list[dict], relations: list[dict]) -> list[dict]:
    rows = []
    for concept in concepts:
        for member in concept.get("members") or []:
            rows.append(
                {
                    "id": f"{member['field_group_id']}->ALIGNED_TO->{concept['id']}",
                    "from": member["field_group_id"],
                    "to": concept["id"],
                    "type": "ALIGNED_TO",
                    "properties": {
                        "project_key": concept["project_key"],
                        "fact_type": "ontology_concept_candidate",
                        "confidence": concept["confidence"],
                        "knowledge_admission": concept["knowledge_admission"],
                        "quality_tier": concept["quality_tier"],
                        "source_type": concept["method"],
                        "inferred": True,
                        "built_at": concept["built_at"],
                    },
                }
            )
    rel_map = {
        "same_concept_candidate": "SAME_CONCEPT_CANDIDATE",
        "same_source_variant": "SAME_SOURCE_VARIANT",
        "derived_variant": "DERIVED_VARIANT",
        "weak_related": "RELATED_CANDIDATE",
    }
    for rel in relations:
        rows.append(
            {
                "id": f"{rel['from_group_id']}->{rel_map.get(rel['relationship_type'], 'RELATED_CANDIDATE')}->{rel['to_group_id']}:{stable_hash(rel['id'], 8)}",
                "from": rel["from_group_id"],
                "to": rel["to_group_id"],
                "type": rel_map.get(rel["relationship_type"], "RELATED_CANDIDATE"),
                "properties": {
                    "project_key": rel["project_key"],
                    "fact_type": "ontology_alignment_relation",
                    "relationship_type": rel["relationship_type"],
                    "score": rel["score"],
                    "confidence": rel["confidence"],
                    "knowledge_admission": rel["knowledge_admission"],
                    "quality_tier": rel["quality_tier"],
                    "evidence_json": json.dumps(rel["evidence"], ensure_ascii=False, sort_keys=True),
                    "source_type": rel["method"],
                    "inferred": True,
                    "built_at": rel["built_at"],
                },
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--project-key", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--max-pairs", type=int, default=12000)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    out_dir = Path(args.output_dir) if args.output_dir else project_dir / "ontology_v2"
    if not (out_dir / "verified_field_groups.jsonl").exists():
        raise SystemExit(f"Missing verified field groups: {out_dir / 'verified_field_groups.jsonl'}")
    concepts, relations, summary = align(project_dir, out_dir, args.project_key, args.max_pairs)
    write_jsonl(out_dir / "concept_candidates.jsonl", concepts)
    write_jsonl(out_dir / "concept_relations.jsonl", relations)
    write_json(out_dir / "concept_alignment_summary.json", summary)
    write_jsonl(out_dir / "concept_graph_nodes.jsonl", graph_nodes(concepts))
    write_jsonl(out_dir / "concept_graph_edges.jsonl", graph_edges(concepts, relations))
    print(json.dumps({"output_dir": str(out_dir), **summary}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
