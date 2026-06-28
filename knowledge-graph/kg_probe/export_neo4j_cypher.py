#!/usr/bin/env python3
"""Export graph JSONL facts to a Neo4j Cypher import script."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SAFE_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA_LINES = [
    "CREATE CONSTRAINT kg_node_id IF NOT EXISTS FOR (n:KGNode) REQUIRE n.id IS UNIQUE;",
    "CREATE CONSTRAINT dataset_name IF NOT EXISTS FOR (n:Dataset) REQUIRE n.name IS UNIQUE;",
    "CREATE CONSTRAINT schedule_task_id IF NOT EXISTS FOR (n:ScheduleTask) REQUIRE n.task_id IS UNIQUE;",
    "CREATE CONSTRAINT sql_statement_id IF NOT EXISTS FOR (n:SqlStatement) REQUIRE n.statement_id IS UNIQUE;",
    "CREATE CONSTRAINT metric_id IF NOT EXISTS FOR (n:Metric) REQUIRE n.metric_id IS UNIQUE;",
    "CREATE CONSTRAINT owner_name IF NOT EXISTS FOR (n:Owner) REQUIRE n.name IS UNIQUE;",
    "CREATE INDEX dataset_layer IF NOT EXISTS FOR (n:Dataset) ON (n.layer);",
    "CREATE INDEX dataset_db_name IF NOT EXISTS FOR (n:Dataset) ON (n.db_name);",
    "CREATE INDEX column_dataset_name IF NOT EXISTS FOR (n:Column) ON (n.dataset, n.name);",
    "CREATE INDEX column_name IF NOT EXISTS FOR (n:Column) ON (n.name);",
    "CREATE INDEX task_type IF NOT EXISTS FOR (n:ScheduleTask) ON (n.task_type);",
    "CREATE INDEX task_layer IF NOT EXISTS FOR (n:ScheduleTask) ON (n.layer);",
    "CREATE INDEX metric_chinese_name IF NOT EXISTS FOR (n:Metric) ON (n.chinese_name);",
    "CREATE INDEX metric_english_name IF NOT EXISTS FOR (n:Metric) ON (n.english_name);",
    "CREATE INDEX fact_build_id IF NOT EXISTS FOR (n:KGNode) ON (n.build_id);",
    "CREATE INDEX fact_type IF NOT EXISTS FOR (n:KGNode) ON (n.fact_type);",
    "CREATE INDEX prompt_template_id IF NOT EXISTS FOR (n:PromptTemplate) ON (n.template_id);",
    "CREATE INDEX prompt_run_generation_id IF NOT EXISTS FOR (n:PromptRun) ON (n.generation_id);",
    "CREATE INDEX evidence_bundle_id IF NOT EXISTS FOR (n:EvidenceBundle) ON (n.bundle_id);",
    "CREATE INDEX code_definition_id IF NOT EXISTS FOR (n:CodeDefinition) ON (n.definition_id);",
    "CREATE INDEX definition_comparison_id IF NOT EXISTS FOR (n:DefinitionComparison) ON (n.comparison_id);",
]


def cypher_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(cypher_value(item) for item in value) + "]"
    if value is None:
        return "null"
    return json.dumps(str(value), ensure_ascii=False)


def props_literal(props: dict) -> str:
    if not props:
        return "{}"
    parts = []
    for key, value in props.items():
        if not SAFE_LABEL_RE.match(key):
            continue
        parts.append(f"{key}: {cypher_value(value)}")
    return "{" + ", ".join(parts) + "}"


def labels_literal(labels: list[str]) -> str:
    safe = [label for label in labels if SAFE_LABEL_RE.match(label)]
    return "".join(f":{label}" for label in safe)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="full")
    args = parser.parse_args()

    base = Path(args.project_dir)
    nodes_path = base / f"{args.prefix}_graph_nodes.jsonl"
    edges_path = base / f"{args.prefix}_graph_edges.jsonl"
    out_path = base / f"{args.prefix}_neo4j_import.cypher"

    lines = [*SCHEMA_LINES, ""]

    for raw in nodes_path.read_text().splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        labels = ["KGNode", *item.get("labels", [])]
        props = {"id": item["id"], **item.get("properties", {})}
        lines.append(f"MERGE (n:KGNode {{id: {cypher_value(item['id'])}}})")
        lines.append(f"SET n{labels_literal(labels)}, n += {props_literal(props)};")

    lines.append("")
    for raw in edges_path.read_text().splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        rel_type = item["type"]
        if not SAFE_LABEL_RE.match(rel_type):
            continue
        props = {"id": item["id"], **item.get("properties", {})}
        lines.append(
            f"MATCH (a:KGNode {{id: {cypher_value(item['from'])}}}), "
            f"(b:KGNode {{id: {cypher_value(item['to'])}}})"
        )
        lines.append(f"MERGE (a)-[r:{rel_type} {{id: {cypher_value(item['id'])}}}]->(b)")
        lines.append(f"SET r += {props_literal(props)};")

    out_path.write_text("\n".join(lines) + "\n")
    print(json.dumps({"cypher_path": str(out_path), "line_count": len(lines)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
