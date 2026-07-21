#!/usr/bin/env python3
"""Batch import graph JSONL facts into Neo4j and run validation queries."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


for LOCAL_USERBASE in [
    Path("/Applications/personal-work/kg-python-userbase/lib/python/site-packages"),
    Path("/Applications/personal-work/kg-python-userbase/lib/python3.9/site-packages"),
]:
    if LOCAL_USERBASE.exists():
        sys.path.insert(0, str(LOCAL_USERBASE))

from neo4j import GraphDatabase  # noqa: E402


SAFE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT kg_node_id IF NOT EXISTS FOR (n:KGNode) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT dataset_project_name IF NOT EXISTS FOR (n:Dataset) REQUIRE (n.project_key, n.name) IS UNIQUE",
    "CREATE CONSTRAINT schedule_task_project_id IF NOT EXISTS FOR (n:ScheduleTask) REQUIRE (n.project_key, n.task_id) IS UNIQUE",
    "CREATE CONSTRAINT sql_statement_project_id IF NOT EXISTS FOR (n:SqlStatement) REQUIRE (n.project_key, n.statement_id) IS UNIQUE",
    "CREATE CONSTRAINT metric_project_id IF NOT EXISTS FOR (n:Metric) REQUIRE (n.project_key, n.metric_id) IS UNIQUE",
    "CREATE CONSTRAINT owner_project_name IF NOT EXISTS FOR (n:Owner) REQUIRE (n.project_key, n.name) IS UNIQUE",
    "CREATE INDEX dataset_layer IF NOT EXISTS FOR (n:Dataset) ON (n.layer)",
    "CREATE INDEX dataset_db_name IF NOT EXISTS FOR (n:Dataset) ON (n.db_name)",
    "CREATE INDEX column_dataset_name IF NOT EXISTS FOR (n:Column) ON (n.dataset, n.name)",
    "CREATE INDEX column_name IF NOT EXISTS FOR (n:Column) ON (n.name)",
    "CREATE INDEX task_type IF NOT EXISTS FOR (n:ScheduleTask) ON (n.task_type)",
    "CREATE INDEX task_layer IF NOT EXISTS FOR (n:ScheduleTask) ON (n.layer)",
    "CREATE INDEX metric_chinese_name IF NOT EXISTS FOR (n:Metric) ON (n.chinese_name)",
    "CREATE INDEX metric_english_name IF NOT EXISTS FOR (n:Metric) ON (n.english_name)",
    "CREATE INDEX fact_build_id IF NOT EXISTS FOR (n:KGNode) ON (n.build_id)",
    "CREATE INDEX fact_type IF NOT EXISTS FOR (n:KGNode) ON (n.fact_type)",
    "CREATE FULLTEXT INDEX kg_entity_search IF NOT EXISTS FOR (n:KGNode) ON EACH [n.id, n.name, n.chinese_name, n.english_name, n.task_name, n.metric_id, n.task_id, n.definition, n.comment]",
]

LEGACY_SINGLE_PROJECT_CONSTRAINTS = [
    "dataset_name",
    "schedule_task_id",
    "sql_statement_id",
    "metric_id",
    "owner_name",
]


def load_jsonl(path: Path):
    with path.open() as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def cypher_label(label: str) -> str:
    if not SAFE_TOKEN.match(label):
        raise ValueError(f"Unsafe label: {label}")
    return f"`{label}`"


def cypher_rel_type(rel_type: str) -> str:
    if not SAFE_TOKEN.match(rel_type):
        raise ValueError(f"Unsafe relationship type: {rel_type}")
    return f"`{rel_type}`"


def chunks(items: list[dict], size: int):
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def neo4j_properties(properties: dict) -> dict:
    """Convert nested JSON values to deterministic strings accepted by Neo4j."""
    normalized = {}
    for key, value in properties.items():
        if isinstance(value, dict) or (
            isinstance(value, list)
            and any(isinstance(item, (dict, list)) for item in value)
        ):
            normalized[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            normalized[key] = value
    return normalized


def execute_write_batches(session, query: str, rows: list[dict], batch_size: int) -> int:
    count = 0
    for batch in chunks(rows, batch_size):
        session.run(query, rows=batch).consume()
        count += len(batch)
    return count


def ensure_schema(session) -> None:
    for name in LEGACY_SINGLE_PROJECT_CONSTRAINTS:
        session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()
    for statement in SCHEMA_STATEMENTS:
        session.run(statement).consume()
    session.run("CALL db.awaitIndexes()").consume()


def clear_database(session, batch_size: int) -> dict:
    started_at = time.time()
    deleted_nodes = 0
    while True:
        result = session.run(
            """
            MATCH (n)
            WITH n LIMIT $batch_size
            DETACH DELETE n
            RETURN count(n) AS deleted
            """,
            batch_size=batch_size,
        ).single()
        deleted = result["deleted"] if result else 0
        if not deleted:
            break
        deleted_nodes += deleted
    return {
        "deleted_nodes": deleted_nodes,
        "clear_elapsed_seconds": round(time.time() - started_at, 2),
    }


def clear_project(session, project_id: str, batch_size: int) -> dict:
    started_at = time.time()
    deleted_nodes = 0
    while True:
        result = session.run(
            """
            MATCH (n:KGNode)
            WHERE n.project_key = $project_id OR n.project_id = $project_id
            WITH n LIMIT $batch_size
            DETACH DELETE n
            RETURN count(n) AS deleted
            """,
            project_id=project_id,
            batch_size=batch_size,
        ).single()
        deleted = result["deleted"] if result else 0
        if not deleted:
            break
        deleted_nodes += deleted
    return {
        "deleted_nodes": deleted_nodes,
        "clear_elapsed_seconds": round(time.time() - started_at, 2),
        "cleared_project_id": project_id,
    }


def namespace_id(project_id: str | None, entity_id: str) -> str:
    if not project_id or entity_id.startswith(f"{project_id}::"):
        return entity_id
    return f"{project_id}::{entity_id}"


def import_graph(
    project_dir: Path,
    prefix: str,
    uri: str,
    user: str,
    password: str,
    batch_size: int,
    project_id: str | None = None,
    replace_project: bool = False,
) -> dict:
    nodes_path = project_dir / f"{prefix}_graph_nodes.jsonl"
    edges_path = project_dir / f"{prefix}_graph_edges.jsonl"
    driver = GraphDatabase.driver(uri, auth=(user, password))
    started_at = time.time()
    with driver.session(database="neo4j") as session:
        if replace_project:
            if not project_id:
                raise ValueError("--replace-project requires --project-id")
            clear_summary = clear_project(session, project_id, batch_size)
        else:
            clear_summary = clear_database(session, batch_size)
        ensure_schema(session)

        node_groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        for item in load_jsonl(nodes_path):
            labels = tuple(sorted(set(item.get("labels", []) + ["KGNode"])))
            original_id = item["id"]
            node_id = namespace_id(project_id, original_id) if replace_project else original_id
            raw_props = {"id": node_id, **item.get("properties", {})}
            if replace_project:
                raw_props["original_id"] = original_id
                raw_props["project_key"] = project_id
                if "Project" in labels:
                    raw_props["project_id"] = project_id
            props = neo4j_properties(raw_props)
            node_groups[labels].append({"id": node_id, "props": props})

        imported_nodes = 0
        for labels, rows in sorted(node_groups.items(), key=lambda pair: pair[0]):
            label_clause = ":".join(cypher_label(label) for label in labels)
            query = f"""
            UNWIND $rows AS row
            MERGE (n:KGNode {{id: row.id}})
            SET n:{label_clause}
            SET n += row.props
            """
            imported_nodes += execute_write_batches(session, query, rows, batch_size)

        edge_groups: dict[str, list[dict]] = defaultdict(list)
        for item in load_jsonl(edges_path):
            original_id = item["id"]
            edge_id = namespace_id(project_id, original_id) if replace_project else original_id
            from_id = namespace_id(project_id, item["from"]) if replace_project else item["from"]
            to_id = namespace_id(project_id, item["to"]) if replace_project else item["to"]
            raw_props = {"id": edge_id, **item.get("properties", {})}
            if replace_project:
                raw_props["original_id"] = original_id
                raw_props["project_key"] = project_id
            props = neo4j_properties(raw_props)
            edge_groups[item["type"]].append(
                {"id": edge_id, "from": from_id, "to": to_id, "props": props}
            )

        imported_edges = 0
        for rel_type, rows in sorted(edge_groups.items()):
            rel = cypher_rel_type(rel_type)
            query = f"""
            UNWIND $rows AS row
            MATCH (a:KGNode {{id: row.from}})
            MATCH (b:KGNode {{id: row.to}})
            MERGE (a)-[r:{rel} {{id: row.id}}]->(b)
            SET r += row.props
            """
            imported_edges += execute_write_batches(session, query, rows, batch_size)

        session.run("CALL db.awaitIndexes()").consume()
    driver.close()
    return {
        **clear_summary,
        "imported_nodes": imported_nodes,
        "imported_edges": imported_edges,
        "elapsed_seconds": round(time.time() - started_at, 2),
    }


def records_to_plain(records):
    return [dict(record) for record in records]


def validate(uri: str, user: str, password: str, project_id: str | None = None) -> dict:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database="neo4j") as session:
        ensure_schema(session)
        schema_count = session.run("SHOW INDEXES YIELD name RETURN count(name) AS count").single()["count"]
        node_filter = " {project_key: $project_id}" if project_id else ""
        params = {"project_id": project_id} if project_id else {}
        node_count = session.run(f"MATCH (n:KGNode{node_filter}) RETURN count(n) AS count", params).single()["count"]
        edge_count = session.run(
            f"MATCH (:KGNode{node_filter})-[r]->(:KGNode{node_filter}) RETURN count(r) AS count",
            params,
        ).single()["count"]
        label_dist = records_to_plain(
            session.run(
                f"MATCH (n:KGNode{node_filter}) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label",
                params,
            )
        )
        rel_dist = records_to_plain(
            session.run(
                f"MATCH (:KGNode{node_filter})-[r]->(:KGNode{node_filter}) RETURN type(r) AS type, count(*) AS count ORDER BY type",
                params,
            )
        )
        missing_endpoints = 0
        dataset_paths = records_to_plain(
            session.run(
                """
                MATCH (d:Dataset)
                WHERE ($project_id IS NULL OR d.project_key = $project_id)
                  AND d.layer IN ['dm', 'dm_index_n']
                WITH d
                ORDER BY size([(d)-[:DATASET_DEPENDS_ON]->() | 1]) DESC
                LIMIT 20
                CALL (d) {
                  MATCH path = (d)-[:DATASET_DEPENDS_ON*1..12]->(src:Dataset)
                  WHERE src.layer IN ['odata', 'pdata']
                  RETURN src.name AS source_dataset, length(path) AS hops
                  LIMIT 1
                }
                RETURN d.name AS dataset, source_dataset, hops
                ORDER BY dataset
                """,
                {"project_id": project_id},
            )
        )
        task_paths = records_to_plain(
            session.run(
                """
                MATCH (:Project)-[:HAS_ENTRY_TASK]->(t:ScheduleTask)
                WHERE $project_id IS NULL OR t.project_key = $project_id
                WITH t LIMIT 20
                CALL (t) {
                  MATCH path = (t)-[:DEPENDS_ON*1..20]->(src:ScheduleTask)
                  WHERE NOT (src)-[:DEPENDS_ON]->(:ScheduleTask)
                  RETURN src.task_id AS terminal_task_id, length(path) AS hops
                  LIMIT 1
                }
                RETURN t.task_id AS task_id, terminal_task_id, hops
                ORDER BY task_id
                """,
                {"project_id": project_id},
            )
        )
        metric_check = session.run(
            """
            MATCH (m:Metric)
            WHERE $project_id IS NULL OR m.project_key = $project_id
            WITH collect(m)[0..20] AS metrics
            UNWIND metrics AS m
            OPTIONAL MATCH (m)-[:STORED_IN]->(:Dataset)
            WITH m, count(*) AS stored
            OPTIONAL MATCH (m)-[:COMPUTED_BY]->(:ScheduleTask)
            WITH m, stored, count(*) AS computed
            RETURN count(m) AS sample_metric_count,
                   sum(CASE WHEN stored > 0 THEN 1 ELSE 0 END) AS metrics_with_storage,
                   sum(CASE WHEN computed > 0 THEN 1 ELSE 0 END) AS metrics_with_compute_task
            """,
            {"project_id": project_id},
        ).single()
        sample_column_lineage = records_to_plain(
            session.run(
                """
                MATCH path = (c:Column)-[:DERIVED_FROM*1..3]->(src:Column)
                WHERE $project_id IS NULL OR c.project_key = $project_id
                RETURN c.dataset AS target_dataset, c.name AS target_column,
                       src.dataset AS source_dataset, src.name AS source_column,
                       length(path) AS hops
                LIMIT 10
                """,
                {"project_id": project_id},
            )
        )
    driver.close()
    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "label_distribution": label_dist,
        "relationship_distribution": rel_dist,
        "missing_edge_endpoint_count": missing_endpoints,
        "dataset_trace_sample": dataset_paths,
        "task_trace_sample": task_paths,
        "metric_check": dict(metric_check),
        "column_lineage_sample_count": len(sample_column_lineage),
        "schema_index_count": schema_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password-file", default="/Applications/personal-work/kg-code-snapshots/neo4j_password.txt")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--project-id", default=None, help="Project id for project-scoped import and validation")
    parser.add_argument("--replace-project", action="store_true", help="Delete and replace only this project_id instead of clearing the whole database")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    password = Path(args.password_file).read_text().strip()
    import_summary = {}
    if not args.skip_import:
        import_summary = import_graph(
            project_dir,
            args.prefix,
            args.uri,
            args.user,
            password,
            args.batch_size,
            project_id=args.project_id,
            replace_project=args.replace_project,
        )
    else:
        import_summary = {"skipped": True}
    validation = validate(args.uri, args.user, password, args.project_id)
    out = project_dir / f"{args.prefix}_neo4j_validation.json"
    result = {"import": import_summary, "validation": validation}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"output": str(out), **import_summary, **validation}, ensure_ascii=False))


if __name__ == "__main__":
    main()
