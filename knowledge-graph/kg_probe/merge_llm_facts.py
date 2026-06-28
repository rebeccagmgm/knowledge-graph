#!/usr/bin/env python3
"""Merge LLM definition facts into graph JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


def stable_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_confidence(value: object) -> str:
    text = str(value or "").strip().lower()
    if "low" in text or "低" in text:
        return "low"
    if "high" in text or "高" in text:
        return "high"
    if "medium" in text or "中" in text:
        return "medium"
    return "medium"


def node(node_id: str, labels: list[str], **props) -> dict:
    clean = {k: v for k, v in props.items() if v not in (None, "", [], {})}
    return {"id": node_id, "labels": labels, "properties": clean}


def edge(edge_id: str, from_id: str, to_id: str, rel_type: str, **props) -> dict:
    clean = {k: v for k, v in props.items() if v not in (None, "", [], {})}
    return {"id": edge_id, "from": from_id, "to": to_id, "type": rel_type, "properties": clean}


def add_common_props(item: dict, *, project_key: str, prefix: str, build_id: str, built_at: str, fact_type: str, confidence: str = "medium") -> dict:
    props = item.setdefault("properties", {})
    props.setdefault("project_key", project_key)
    props.setdefault("graph_prefix", prefix)
    props.setdefault("build_id", build_id)
    props.setdefault("built_at", built_at)
    props.setdefault("fact_type", fact_type)
    props.setdefault("confidence", confidence)
    props.setdefault("inferred", False)
    return item


def template_nodes(template_path: Path) -> list[dict]:
    raw = json.loads(template_path.read_text())
    rows = []
    for item in raw["templates"]:
        template_hash = stable_hash(item)
        rows.append(
            node(
                f"prompt_template:{item['template_id']}:{item['version']}",
                ["PromptTemplate"],
                template_id=item["template_id"],
                version=item["version"],
                purpose=item.get("purpose"),
                template_hash=template_hash,
                system=item.get("system"),
                user=item.get("user"),
            )
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--output-prefix", default="strategy_llm")
    parser.add_argument("--templates", default=str(Path(__file__).with_name("llm_prompt_templates.json")))
    args = parser.parse_args()

    base = Path(args.project_dir)
    llm_dir = base / "llm"
    nodes = {item["id"]: item for item in load_jsonl(base / f"{args.prefix}_graph_nodes.jsonl")}
    edges = {item["id"]: item for item in load_jsonl(base / f"{args.prefix}_graph_edges.jsonl")}
    evidences = {item["bundle_id"]: item for item in load_jsonl(llm_dir / "evidence_bundles.jsonl")}
    requests = {item["generation_id"]: item for item in load_jsonl(llm_dir / "code_definition_requests.jsonl")}
    definition_runs = load_jsonl(llm_dir / "code_definitions.jsonl")
    definitions = [item for item in definition_runs if item.get("status") == "ok"]
    comparisons = {item["definition_id"]: item for item in load_jsonl(llm_dir / "definition_comparisons.jsonl")}

    project_keys = {item.get("properties", {}).get("project_key") for item in nodes.values()}
    project_key = sorted(x for x in project_keys if x)[0] if any(project_keys) else base.name
    built_at = datetime.now().isoformat(timespec="seconds")
    build_id = stable_hash({"prefix": args.output_prefix, "built_at": built_at, "project_key": project_key})[:16]

    for tmpl in template_nodes(Path(args.templates)):
        add_common_props(tmpl, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="llm_prompt_template", confidence="high")
        nodes[tmpl["id"]] = tmpl

    for bundle_id, bundle in evidences.items():
        payload = bundle.get("payload", {})
        evidence_node = node(
            bundle_id,
            ["EvidenceBundle"],
            bundle_id=bundle_id,
            metric_id=bundle.get("metric_id"),
            input_hash=bundle.get("input_hash"),
            evidence_node_count=len(payload.get("evidence_node_ids", [])),
            evidence_edge_count=len(payload.get("evidence_edge_ids", [])),
            sql_statement_count=len(payload.get("sql_statements", [])),
            read_table_count=len(payload.get("read_tables", [])),
            write_table_count=len(payload.get("write_tables", [])),
            registered_definition_count=len(payload.get("registered_definitions", [])),
        )
        add_common_props(evidence_node, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="llm_evidence", confidence="high")
        nodes[evidence_node["id"]] = evidence_node
        metric_node_id = bundle.get("metric_node_id")
        if metric_node_id in nodes:
            eid = f"{metric_node_id}->HAS_EVIDENCE_BUNDLE->{bundle_id}"
            edges[eid] = edge(eid, metric_node_id, bundle_id, "HAS_EVIDENCE_BUNDLE", source_type="llm_evidence_build")
        for evidenced_id in payload.get("evidence_node_ids", []):
            if evidenced_id not in nodes:
                continue
            rel_type = "EVIDENCES"
            labels = set(nodes[evidenced_id].get("labels", []))
            if "SqlStatement" in labels:
                rel_type = "EVIDENCES_SQL"
            elif "Dataset" in labels:
                rel_type = "EVIDENCES_DATASET"
            elif "Column" in labels:
                rel_type = "EVIDENCES_COLUMN"
            eid = f"{bundle_id}->{rel_type}->{evidenced_id}"
            edges[eid] = edge(eid, bundle_id, evidenced_id, rel_type, source_type="llm_evidence_build")

    for row in definition_runs:
        request = requests.get(row["generation_id"], {})
        model_id = f"model_version:{row.get('provider')}:{row.get('model')}"
        model_node = node(model_id, ["ModelVersion"], provider=row.get("provider"), model=row.get("model"))
        add_common_props(model_node, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="llm_model", confidence="high")
        nodes[model_id] = model_node

        prompt_node = node(
            row["generation_id"],
            ["PromptRun"],
            generation_id=row["generation_id"],
            kind="code_definition",
            provider=row.get("provider"),
            model=row.get("model"),
            status=row.get("status"),
            error=row.get("error"),
            input_hash=row.get("input_hash"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
        )
        add_common_props(prompt_node, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="llm_prompt_run", confidence="medium")
        nodes[prompt_node["id"]] = prompt_node

        template_node_id = f"prompt_template:{row['prompt_template_id']}:{row['prompt_template_version']}"
        for rel_type, to_id in [
            ("USED_EVIDENCE", row["evidence_bundle_id"]),
            ("USED_MODEL", model_id),
            ("USED_TEMPLATE", template_node_id),
        ]:
            if to_id in nodes:
                eid = f"{row['generation_id']}->{rel_type}->{to_id}"
                edges[eid] = edge(eid, row["generation_id"], to_id, rel_type, source_type="llm_definition_run")

        # Failed runs remain auditable as PromptRun nodes, but must not become facts.
        if row.get("status") != "ok":
            continue

        definition = row.get("definition", {})
        definition_node = node(
            row["definition_id"],
            ["CodeDefinition"],
            definition_id=row["definition_id"],
            metric_id=row.get("metric_id"),
            summary=definition.get("summary"),
            calculation_logic=definition.get("calculation_logic"),
            target_table=definition.get("target_table"),
            source_tables=definition.get("source_tables"),
            filters=definition.get("filters"),
            grain=definition.get("grain"),
            schedule_tasks=definition.get("schedule_tasks"),
            llm_confidence=definition.get("confidence"),
            uncertainties=definition.get("uncertainties"),
            definition_json=compact_json(definition),
            status=row.get("status"),
        )
        add_common_props(definition_node, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="metric_code_definition", confidence=normalize_confidence(definition.get("confidence")))
        nodes[definition_node["id"]] = definition_node

        rels = [
            (row["metric_node_id"], "HAS_CODE_DEFINITION", row["definition_id"]),
            (row["definition_id"], "GENERATED_BY", row["generation_id"]),
        ]
        for from_id, rel_type, to_id in rels:
            if from_id in nodes and to_id in nodes:
                eid = f"{from_id}->{rel_type}->{to_id}"
                edges[eid] = edge(eid, from_id, to_id, rel_type, source_type="llm_definition")

        comparison = comparisons.get(row["definition_id"])
        if comparison:
            comp = comparison.get("comparison", {})
            compare_model_id = f"model_version:{comparison.get('provider')}:{comparison.get('model')}"
            compare_model_node = node(
                compare_model_id,
                ["ModelVersion"],
                provider=comparison.get("provider"),
                model=comparison.get("model"),
            )
            add_common_props(compare_model_node, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="llm_model", confidence="high")
            nodes[compare_model_id] = compare_model_node

            compare_prompt_id = "prompt_run:compare:" + stable_hash(
                {
                    "comparison_id": comparison["comparison_id"],
                    "definition_id": row["definition_id"],
                    "evidence_bundle_id": row["evidence_bundle_id"],
                    "template_id": comparison["prompt_template_id"],
                    "template_version": comparison["prompt_template_version"],
                }
            )[:24]
            compare_prompt_node = node(
                compare_prompt_id,
                ["PromptRun"],
                generation_id=compare_prompt_id,
                kind="definition_comparison",
                provider=comparison.get("provider"),
                model=comparison.get("model"),
                status=comparison.get("status"),
                error=comparison.get("error"),
                started_at=comparison.get("started_at"),
                finished_at=comparison.get("finished_at"),
            )
            add_common_props(compare_prompt_node, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="llm_prompt_run", confidence="medium")
            nodes[compare_prompt_id] = compare_prompt_node

            comp_node = node(
                comparison["comparison_id"],
                ["DefinitionComparison"],
                comparison_id=comparison["comparison_id"],
                metric_id=comparison.get("metric_id"),
                status=comp.get("status"),
                agreement_points=comp.get("agreement_points"),
                conflict_points=comp.get("conflict_points"),
                missing_in_registry=comp.get("missing_in_registry"),
                insufficient_code_evidence=comp.get("insufficient_code_evidence"),
                recommended_definition=comp.get("recommended_definition"),
                llm_confidence=comp.get("confidence"),
                comparison_json=compact_json(comp),
            )
            add_common_props(comp_node, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="metric_definition_comparison", confidence=normalize_confidence(comp.get("confidence")))
            nodes[comp_node["id"]] = comp_node
            compare_template_node_id = f"prompt_template:{comparison['prompt_template_id']}:{comparison['prompt_template_version']}"
            for from_id, rel_type, to_id in [
                (row["definition_id"], "HAS_COMPARISON", comparison["comparison_id"]),
                (comparison["comparison_id"], "COMPARES_CODE_DEFINITION", row["definition_id"]),
                (comparison["comparison_id"], "GENERATED_BY", compare_prompt_id),
                (compare_prompt_id, "USED_TEMPLATE", compare_template_node_id),
                (compare_prompt_id, "USED_MODEL", compare_model_id),
                (compare_prompt_id, "USED_EVIDENCE", row["evidence_bundle_id"]),
            ]:
                if from_id in nodes and to_id in nodes:
                    eid = f"{from_id}->{rel_type}->{to_id}"
                    edges[eid] = edge(eid, from_id, to_id, rel_type, source_type="llm_comparison")
            bundle = evidences.get(row["evidence_bundle_id"], {})
            for reg in bundle.get("payload", {}).get("registered_definitions", []):
                reg_id = reg.get("id")
                if reg_id in nodes:
                    eid = f"{comparison['comparison_id']}->COMPARES_REGISTERED_DEFINITION->{reg_id}"
                    edges[eid] = edge(eid, comparison["comparison_id"], reg_id, "COMPARES_REGISTERED_DEFINITION", source_type="llm_comparison")

    for item in nodes.values():
        if item.get("properties", {}).get("graph_prefix") == args.output_prefix:
            continue
    for item in edges.values():
        if item.get("properties", {}).get("graph_prefix") == args.output_prefix:
            continue
        add_common_props(item, project_key=project_key, prefix=args.prefix, build_id=item.get("properties", {}).get("build_id", build_id), built_at=item.get("properties", {}).get("built_at", built_at), fact_type=item.get("properties", {}).get("fact_type", "relationship"), confidence=item.get("properties", {}).get("confidence", "medium"))

    for item in list(edges.values()):
        if item.get("properties", {}).get("graph_prefix") == args.output_prefix:
            continue
        if item["id"] in edges and item.get("properties"):
            continue
    for edge_item in edges.values():
        if edge_item.get("properties", {}).get("graph_prefix"):
            continue
        add_common_props(edge_item, project_key=project_key, prefix=args.output_prefix, build_id=build_id, built_at=built_at, fact_type="llm_relationship", confidence="medium")

    out_nodes = base / f"{args.output_prefix}_graph_nodes.jsonl"
    out_edges = base / f"{args.output_prefix}_graph_edges.jsonl"
    write_jsonl(out_nodes, list(nodes.values()))
    write_jsonl(out_edges, list(edges.values()))
    summary = {
        "output_prefix": args.output_prefix,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "evidence_bundle_count": len(evidences),
        "code_definition_count": len(definitions),
        "failed_definition_run_count": len(definition_runs) - len(definitions),
        "comparison_count": len(comparisons),
        "nodes_path": str(out_nodes),
        "edges_path": str(out_edges),
    }
    (base / f"{args.output_prefix}_graph_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
