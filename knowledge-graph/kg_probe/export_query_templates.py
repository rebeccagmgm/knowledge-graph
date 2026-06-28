#!/usr/bin/env python3
"""Write reusable Neo4j query templates for the generated KG schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TEMPLATE = """// Knowledge graph query templates.
// Replace parameter values before running in cypher-shell / Neo4j Browser.

// 1. Upstream schedule lineage for one task.
:param task_id => "236334";
MATCH path = (t:ScheduleTask {task_id: $task_id})-[:DEPENDS_ON*1..20]->(up:ScheduleTask)
WHERE NOT (up)-[:DEPENDS_ON]->(:ScheduleTask)
RETURN path
LIMIT 20;

// 2. Dataset upstream lineage through SQL READS/WRITES facts.
:param dataset_name => "dm_index_n.example_table";
MATCH path = (target:Dataset {name: $dataset_name})<-[:WRITES]-(:SqlStatement)-[:READS]->(src:Dataset)
RETURN path
LIMIT 50;

// 3. Multi-hop dataset upstream lineage using materialized table-level dependency.
// If DATASET_DEPENDS_ON is materialized later, this query becomes the preferred one.
:param dataset_name => "dm_index_n.example_table";
MATCH path = (target:Dataset {name: $dataset_name})-[:DATASET_DEPENDS_ON*1..12]->(src:Dataset)
WHERE src.layer IN ["odata", "pdata"]
RETURN path
LIMIT 50;

// 4. Metric registry to storage table, compute task, and definition.
:param metric_name => "打新次数";
MATCH (m:Metric)
WHERE m.chinese_name = $metric_name OR m.abbreviation = $metric_name OR m.english_name = $metric_name
OPTIONAL MATCH (m)-[:STORED_IN]->(d:Dataset)
OPTIONAL MATCH (m)-[:COMPUTED_BY]->(t:ScheduleTask)
OPTIONAL MATCH (m)-[:HAS_DEFINITION]->(def:MetricDefinition)
RETURN m, d, t, def
LIMIT 20;

// 5. Column-level lineage for one dataset column.
:param dataset_name => "dm_index_n.example_table";
:param column_name => "example_column";
MATCH path = (c:Column {dataset: $dataset_name, name: $column_name})-[:DERIVED_FROM*1..8]->(src:Column)
RETURN path
LIMIT 50;

// 6. Impact analysis from a source dataset to downstream SQL outputs.
:param source_dataset => "odata.example_table";
MATCH path = (src:Dataset {name: $source_dataset})<-[:READS]-(:SqlStatement)-[:WRITES]->(downstream:Dataset)
RETURN path
LIMIT 50;

// 7. Owner responsibility view.
:param owner_name => "owner";
MATCH (o:Owner {name: $owner_name})-[:OWNS]->(n)
RETURN labels(n) AS labels, n
LIMIT 100;

// 8. Fact confidence distribution.
MATCH ()-[r]->()
RETURN r.fact_type AS fact_type, r.confidence AS confidence, count(*) AS count
ORDER BY fact_type, confidence;

// 9. Latest build id and graph scale.
MATCH (p:Project)
RETURN p.project_id AS project_id, p.build_id AS build_id, p.built_at AS built_at;

MATCH (n)
RETURN labels(n) AS labels, count(*) AS count
ORDER BY count DESC;

// 10. Datasets without exact schema columns.
MATCH (d:Dataset)
WHERE NOT (d)-[:HAS_COLUMN]->(:Column)
RETURN d.name AS dataset, d.layer AS layer, d.dms_exact_count AS dms_exact_count
ORDER BY layer, dataset
LIMIT 100;

// 11. Metrics without compute task.
MATCH (m:Metric)
WHERE NOT (m)-[:COMPUTED_BY]->(:ScheduleTask)
RETURN m.chinese_name AS chinese_name, m.english_name AS english_name, m.dataset AS dataset, m.horae_task_id AS horae_task_id
LIMIT 100;
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    out = project_dir / f"{args.prefix}_query_templates.cypher"
    out.write_text(TEMPLATE)
    print(json.dumps({"query_templates_path": str(out), "line_count": len(TEMPLATE.splitlines())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
