#!/usr/bin/env python3
"""Export Neo4j constraints and indexes for the KG graph model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SCHEMA_CYPHER = """// Neo4j schema for the generated knowledge graph.
CREATE CONSTRAINT kg_node_id IF NOT EXISTS FOR (n:KGNode) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT dataset_name IF NOT EXISTS FOR (n:Dataset) REQUIRE n.name IS UNIQUE;
CREATE CONSTRAINT schedule_task_id IF NOT EXISTS FOR (n:ScheduleTask) REQUIRE n.task_id IS UNIQUE;
CREATE CONSTRAINT sql_statement_id IF NOT EXISTS FOR (n:SqlStatement) REQUIRE n.statement_id IS UNIQUE;
CREATE CONSTRAINT metric_id IF NOT EXISTS FOR (n:Metric) REQUIRE n.metric_id IS UNIQUE;
CREATE CONSTRAINT owner_name IF NOT EXISTS FOR (n:Owner) REQUIRE n.name IS UNIQUE;

CREATE INDEX dataset_layer IF NOT EXISTS FOR (n:Dataset) ON (n.layer);
CREATE INDEX dataset_db_name IF NOT EXISTS FOR (n:Dataset) ON (n.db_name);
CREATE INDEX column_dataset_name IF NOT EXISTS FOR (n:Column) ON (n.dataset, n.name);
CREATE INDEX column_name IF NOT EXISTS FOR (n:Column) ON (n.name);
CREATE INDEX task_type IF NOT EXISTS FOR (n:ScheduleTask) ON (n.task_type);
CREATE INDEX task_layer IF NOT EXISTS FOR (n:ScheduleTask) ON (n.layer);
CREATE INDEX metric_chinese_name IF NOT EXISTS FOR (n:Metric) ON (n.chinese_name);
CREATE INDEX metric_english_name IF NOT EXISTS FOR (n:Metric) ON (n.english_name);
CREATE INDEX fact_build_id IF NOT EXISTS FOR (n:KGNode) ON (n.build_id);
CREATE INDEX fact_type IF NOT EXISTS FOR (n:KGNode) ON (n.fact_type);
CREATE INDEX prompt_template_id IF NOT EXISTS FOR (n:PromptTemplate) ON (n.template_id);
CREATE INDEX prompt_run_generation_id IF NOT EXISTS FOR (n:PromptRun) ON (n.generation_id);
CREATE INDEX evidence_bundle_id IF NOT EXISTS FOR (n:EvidenceBundle) ON (n.bundle_id);
CREATE INDEX code_definition_id IF NOT EXISTS FOR (n:CodeDefinition) ON (n.definition_id);
CREATE INDEX definition_comparison_id IF NOT EXISTS FOR (n:DefinitionComparison) ON (n.comparison_id);
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    out = project_dir / f"{args.prefix}_neo4j_schema.cypher"
    out.write_text(SCHEMA_CYPHER, encoding="utf-8")
    print(json.dumps({"schema_path": str(out), "line_count": len(SCHEMA_CYPHER.splitlines())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
