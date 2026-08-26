#!/usr/bin/env python3
"""Build graph-ready JSONL facts from collected project artifacts."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import datetime
from pathlib import Path
import re

HTML_TAG_RE = re.compile(r"<[^>]+>")


def load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def node(node_id: str, labels: list[str], **props) -> dict:
    clean = {k: v for k, v in props.items() if v not in (None, "", [], {})}
    return {"id": node_id, "labels": labels, "properties": clean}


def edge(edge_id: str, from_id: str, to_id: str, rel_type: str, **props) -> dict:
    clean = {k: v for k, v in props.items() if v not in (None, "", [], {})}
    return {
        "id": edge_id,
        "from": from_id,
        "to": to_id,
        "type": rel_type,
        "properties": clean,
    }


def clean_html_text(value: object) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    return HTML_TAG_RE.sub("", text).strip()


def clean_identifier(value: object) -> str:
    text = clean_html_text(value)
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    return text.strip("_").lower()


def generated_expression_id(statement_id: str, projection_ordinal: int | str, expression_sql: str) -> str:
    digest = hashlib.sha256((expression_sql or "").encode("utf-8")).hexdigest()[:16]
    return f"generated_expression:{statement_id}:{projection_ordinal}:{digest}"


def build_id_for(base: Path, prefix: str, lineage_project_key: str) -> str:
    raw = f"{lineage_project_key}|{prefix}|{datetime.now().isoformat(timespec='seconds')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def confidence_for_edge(edge_item: dict) -> str:
    props = edge_item.get("properties", {})
    rel_type = edge_item.get("type", "")
    source_type = props.get("source_type", "")
    source_resolution = props.get("source_resolution", "")
    if rel_type in {"DEPENDS_ON", "HAS_ENTRY_TASK", "HAS_RUNTIME_LOG", "HAS_COLUMN", "STORED_IN", "HAS_DEFINITION"}:
        return "high"
    if rel_type in {"READS", "WRITES"}:
        return "high" if "sqlglot" in source_type else "medium"
    if rel_type == "DATASET_DEPENDS_ON":
        return "high" if source_type == "sql_read_write" else "medium"
    if rel_type == "DERIVED_FROM":
        if source_resolution in {"table_alias", "single_read_dataset", "schema_unique_column", "schema_star_expand"}:
            return "high" if source_resolution == "table_alias" else "medium"
        if str(source_resolution).startswith("cte_"):
            return "medium"
        return "low"
    if rel_type == "INFLUENCED_BY":
        return "medium"
    if rel_type == "GENERATED_BY_EXPRESSION":
        return "medium"
    if rel_type in {"PRODUCES", "CONSUMES", "COMPUTED_BY", "OWNS", "BELONGS_TO_LAYER", "EMITS_SQL"}:
        return "medium"
    return "unknown"


def fact_type_for_node(node_item: dict) -> str:
    labels = set(node_item.get("labels", []))
    if "ScheduleTask" in labels:
        return "schedule_task"
    if "Dataset" in labels:
        return "data_asset"
    if "Column" in labels:
        return "schema_column"
    if "Metric" in labels:
        return "business_metric"
    if "MetricDefinition" in labels:
        return "metric_definition"
    if "SqlStatement" in labels:
        return "code_statement"
    if "GeneratedExpression" in labels:
        return "generated_expression"
    if "RuntimeLog" in labels:
        return "runtime_evidence"
    if "Owner" in labels:
        return "owner"
    if "DataLayer" in labels:
        return "data_layer"
    if "Project" in labels:
        return "project"
    return "unknown"


def fact_type_for_edge(edge_item: dict) -> str:
    rel_type = edge_item.get("type", "")
    if rel_type in {"DEPENDS_ON", "HAS_ENTRY_TASK"}:
        return "schedule_lineage"
    if rel_type in {"READS", "WRITES", "DATASET_DEPENDS_ON", "PRODUCES", "CONSUMES"}:
        return "table_lineage"
    if rel_type in {"DERIVED_FROM", "INFLUENCED_BY", "HAS_COLUMN", "GENERATED_BY_EXPRESSION"}:
        return "column_lineage"
    if rel_type in {"STORED_IN", "COMPUTED_BY", "HAS_DEFINITION"}:
        return "metric_lineage"
    if rel_type in {"OWNS"}:
        return "ownership"
    if rel_type in {"EMITS_SQL", "HAS_RUNTIME_LOG"}:
        return "evidence"
    if rel_type in {"BELONGS_TO_LAYER"}:
        return "classification"
    return "relationship"


def quality_profile(props: dict) -> dict:
    confidence = props.get("confidence", "medium")
    confidence_score = {"high": 35, "medium": 25, "low": 10, "unknown": 0}.get(str(confidence).lower(), 0)
    source_system = str(props.get("source_system") or "").lower()
    source_score = 25 if source_system in {"horae", "szconnector", "sqlglot"} else (15 if source_system else 8)
    evidence_score = 20 if (props.get("statement_id") or props.get("task_id") or props.get("evidence_from_id")) else 8
    inferred = bool(props.get("inferred"))
    inferred_penalty = 10 if inferred else 0
    review_bonus = 5 if props.get("review_status") == "confirmed" else 0
    score = max(0, min(100, confidence_score + source_score + evidence_score + review_bonus - inferred_penalty))
    if score >= 75:
        tier = "high_quality"
        admission = "accepted"
    elif score >= 50:
        tier = "usable_with_context"
        admission = "accepted_with_inference" if inferred else "accepted"
    elif score >= 30:
        tier = "candidate"
        admission = "needs_review"
    else:
        tier = "low_quality"
        admission = "temporary_context"
    return {
        "quality_score": score,
        "quality_tier": tier,
        "knowledge_admission": admission,
    }


def decorate_facts(
    nodes: dict[str, dict],
    edges: dict[str, dict],
    *,
    build_id: str,
    project_key: str,
    prefix: str,
    built_at: str,
) -> None:
    for node_item in nodes.values():
        props = node_item.setdefault("properties", {})
        props.setdefault("fact_type", fact_type_for_node(node_item))
        props.setdefault("project_key", project_key)
        props.setdefault("graph_prefix", prefix)
        props.setdefault("build_id", build_id)
        props.setdefault("built_at", built_at)
        props.setdefault("confidence", "high" if props.get("source_system") in {"horae", "szconnector"} else "medium")
        props.setdefault("inferred", props.get("source_type") in {"inferred", "sql_lineage_inferred"})
        props.update({key: value for key, value in quality_profile(props).items() if key not in props})
    for edge_item in edges.values():
        props = edge_item.setdefault("properties", {})
        props.setdefault("fact_type", fact_type_for_edge(edge_item))
        props.setdefault("project_key", project_key)
        props.setdefault("graph_prefix", prefix)
        props.setdefault("build_id", build_id)
        props.setdefault("built_at", built_at)
        props.setdefault("confidence", confidence_for_edge(edge_item))
        props.setdefault("inferred", props.get("source_type") in {"schedule_dependency_outputs", "dataset_name_rule", "task_detail_sync_info", "sql_lineage_inferred"})
        props.setdefault("evidence_from_id", edge_item.get("from"))
        props.setdefault("evidence_to_id", edge_item.get("to"))
        props.update({key: value for key, value in quality_profile(props).items() if key not in props})


def owner_ids(raw: str) -> list[str]:
    if not raw:
        return []
    raw = clean_html_text(raw)
    normalized = raw.replace("，", ",").replace("/", ",").replace("、", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def person_names(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return owner_ids(raw)
    if isinstance(raw, list):
        names = []
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name") or item.get("userName") or item.get("code")
                if name:
                    names.append(clean_html_text(name))
            elif item:
                names.append(clean_html_text(item))
        return names
    return []


def code_value(raw) -> str:
    if isinstance(raw, dict):
        return clean_html_text(raw.get("code") or raw.get("name") or raw.get("value") or "")
    return clean_html_text(raw) if raw is not None else ""


def dataset_layer(dataset: str) -> str:
    if dataset.startswith("odata"):
        return "odata"
    if dataset.startswith("pdata"):
        return "pdata"
    if dataset.startswith("dm_index_n."):
        return "dm_index_n"
    if dataset.startswith("dm") or dataset.startswith("dm."):
        return "dm"
    return "other"


def is_dataset_name(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][\w]*\.[A-Za-z_][\w]*$", value or ""))


def add_dataset_node(nodes: dict, edges: dict, dataset: str, source_type: str = "inferred") -> str:
    dataset = dataset.lower()
    dataset_id = f"dataset:{dataset}"
    existing = nodes.get(dataset_id, {"properties": {}})
    props = existing.get("properties", {})
    props.setdefault("name", dataset)
    props.setdefault("layer", dataset_layer(dataset))
    props.setdefault("source_type", source_type)
    nodes[dataset_id] = {"id": dataset_id, "labels": ["Dataset"], "properties": props}
    layer_id = f"layer:{props.get('layer', dataset_layer(dataset))}"
    edges.setdefault(
        f"{dataset_id}->BELONGS_TO_LAYER->{layer_id}",
        edge(
            f"{dataset_id}->BELONGS_TO_LAYER->{layer_id}",
            dataset_id,
            layer_id,
            "BELONGS_TO_LAYER",
            inferred_by="dataset_name_rule",
        ),
    )
    return dataset_id


def add_column_node(
    nodes: dict,
    edges: dict,
    dataset: str,
    column: str,
    source_type: str = "sql_lineage_inferred",
) -> str:
    dataset = dataset.lower()
    column = re.sub(r"[^a-zA-Z0-9_]+", "_", (column or "").strip()).strip("_").lower()
    dataset_id = add_dataset_node(nodes, edges, dataset, source_type=source_type)
    column_id = f"column:{dataset}.{column}"
    existing = nodes.get(column_id, {"properties": {}})
    props = existing.get("properties", {})
    props.setdefault("name", column)
    props.setdefault("dataset", dataset)
    props.setdefault("source_system", "sqlglot")
    props.setdefault("source_type", source_type)
    nodes[column_id] = {"id": column_id, "labels": ["Column"], "properties": props}
    edges.setdefault(
        f"{dataset_id}->HAS_COLUMN->{column_id}",
        edge(
            f"{dataset_id}->HAS_COLUMN->{column_id}",
            dataset_id,
            column_id,
            "HAS_COLUMN",
            source_system="sqlglot",
            source_type=source_type,
        ),
    )
    return column_id


def infer_task_datasets(task_node: dict, detail: dict) -> tuple[set[str], set[str]]:
    task_type = detail.get("task_type", "")
    sync_info = detail.get("sync_info") or {}
    task_name = (task_node.get("task_name") or detail.get("description") or "").strip()
    produces: set[str] = set()
    consumes: set[str] = set()
    is_hive_to_external = task_type.startswith("hive2")

    if is_dataset_name(task_name) and not is_hive_to_external:
        produces.add(task_name.lower())

    hive_db = (sync_info.get("Hive源库") or "").strip()
    hive_table = (sync_info.get("Hive源表") or "").strip()
    hive_dataset = f"{hive_db}.{hive_table}".lower() if hive_db and hive_table else ""

    if hive_dataset and is_dataset_name(hive_dataset):
        if task_type in {
            "mysql2hive",
            "oracle2hive",
            "postgre2hive",
            "mongo2hive",
            "sqlserver2hive",
            "file2hive",
            "hdfs2hive",
        }:
            produces.add(hive_dataset)
        elif is_hive_to_external:
            consumes.add(hive_dataset)

    target = (sync_info.get("目标库表") or "").strip()
    if is_dataset_name(target):
        produces.add(target.lower())

    return consumes, produces


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="full")
    args = parser.parse_args()

    base = Path(args.project_dir)
    lineage = load(base / "lineage.json", {})
    details = load(base / "task_details.json", [])
    statements = load(base / f"{args.prefix}_sql_statements.json", [])
    datasets = load(base / f"{args.prefix}_datasets.json", [])
    dataset_edges = load(base / f"{args.prefix}_dataset_edges.json", [])
    column_lineage = load(base / f"{args.prefix}_column_lineage.json", [])
    column_influence = load(base / f"{args.prefix}_column_influence.json", [])
    logs = load(base / "log_artifacts_full.json", load(base / "log_artifacts.json", []))
    dms_records = load(base / "sz_metadata" / "dataset_dms.json", [])
    indicator_records = load(base / "sz_metadata" / "indicator_registry.json", [])

    lineage_project_key = lineage.get("project_id") or lineage.get("root_task_id") or base.name
    project_id = f"project:{lineage_project_key}"
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    now = datetime.now().isoformat(timespec="seconds")
    build_id = build_id_for(base, args.prefix, lineage_project_key)

    nodes[project_id] = node(
        project_id,
        ["Project"],
        root_task_id=lineage.get("root_task_id"),
        root_task_ids=lineage.get("root_task_ids", []),
        project_id=lineage.get("project_id", lineage_project_key),
        project_dir=str(base),
        built_at=now,
        build_id=build_id,
        graph_prefix=args.prefix,
    )

    for layer in ["odata", "pdata", "dm_index_n", "dm", "other", "root_unknown"]:
        layer_id = f"layer:{layer}"
        nodes[layer_id] = node(layer_id, ["DataLayer"], name=layer)

    details_by_task = {item["task_id"]: item for item in details}
    logs_by_task = {item["task_id"]: item for item in logs}
    dms_by_dataset = {item["dataset"]: item for item in dms_records}
    indicators_by_dataset = {item["dataset"]: item for item in indicator_records}

    for item in lineage.get("nodes", []):
        task_id = item["task_id"]
        detail = details_by_task.get(task_id, {})
        task_node_id = f"task:{task_id}"
        nodes[task_node_id] = node(
            task_node_id,
            ["ScheduleTask"],
            task_id=task_id,
            task_name=item.get("task_name") or detail.get("description", ""),
            topic=item.get("topic_name") or detail.get("topic", ""),
            layer=item.get("layer", ""),
            depth=item.get("depth", 0),
            task_type=detail.get("task_type", ""),
            cycle=detail.get("cycle", ""),
            cluster=detail.get("cluster", ""),
            hive_db=detail.get("hive_db", ""),
            source=detail.get("source", ""),
            script_path=detail.get("script_path", ""),
            source_system="horae",
        )
        if item.get("is_root"):
            edges[f"{project_id}->HAS_ENTRY_TASK->{task_node_id}"] = edge(
                f"{project_id}->HAS_ENTRY_TASK->{task_node_id}",
                project_id,
                task_node_id,
                "HAS_ENTRY_TASK",
            )

        layer_id = f"layer:{item.get('layer', 'other')}"
        edges[f"{task_node_id}->BELONGS_TO_LAYER->{layer_id}"] = edge(
            f"{task_node_id}->BELONGS_TO_LAYER->{layer_id}",
            task_node_id,
            layer_id,
            "BELONGS_TO_LAYER",
            inferred_by="task_name_rule",
        )

        for owner in owner_ids(item.get("in_charge") or detail.get("owners", "")):
            owner_node_id = f"owner:{owner}"
            nodes[owner_node_id] = node(owner_node_id, ["Owner"], name=owner)
            edges[f"{owner_node_id}->OWNS->{task_node_id}"] = edge(
                f"{owner_node_id}->OWNS->{task_node_id}",
                owner_node_id,
                task_node_id,
                "OWNS",
                source_system="horae",
            )

        consumes, produces = infer_task_datasets(item, detail)
        for dataset in sorted(consumes):
            dataset_id = add_dataset_node(nodes, edges, dataset, source_type="task_detail_sync_info")
            edges[f"{task_node_id}->CONSUMES->{dataset_id}"] = edge(
                f"{task_node_id}->CONSUMES->{dataset_id}",
                task_node_id,
                dataset_id,
                "CONSUMES",
                source_system="horae",
                source_type="task_detail_sync_info",
            )
        for dataset in sorted(produces):
            dataset_id = add_dataset_node(nodes, edges, dataset, source_type="task_detail_sync_info")
            edges[f"{task_node_id}->PRODUCES->{dataset_id}"] = edge(
                f"{task_node_id}->PRODUCES->{dataset_id}",
                task_node_id,
                dataset_id,
                "PRODUCES",
                source_system="horae",
                source_type="task_detail_sync_info",
            )

        log_info = logs_by_task.get(task_id)
        if log_info:
            log_id = f"log:{task_id}:{log_info.get('run_date', '')[:10]}"
            nodes[log_id] = node(
                log_id,
                ["RuntimeLog"],
                task_id=task_id,
                path=log_info.get("path"),
                run_date=log_info.get("run_date"),
                bytes=log_info.get("bytes"),
                line_count=log_info.get("line_count"),
            )
            edges[f"{task_node_id}->HAS_RUNTIME_LOG->{log_id}"] = edge(
                f"{task_node_id}->HAS_RUNTIME_LOG->{log_id}",
                task_node_id,
                log_id,
                "HAS_RUNTIME_LOG",
            )

    for rel in lineage.get("edges", []):
        upstream = f"task:{rel['from_task_id']}"
        downstream = f"task:{rel['to_task_id']}"
        edge_id = f"{downstream}->DEPENDS_ON->{upstream}"
        edges[edge_id] = edge(
            edge_id,
            downstream,
            upstream,
            "DEPENDS_ON",
            source_system="horae",
            depth_from_root=rel.get("depth_from_root"),
        )

    for item in datasets:
        dataset_id = add_dataset_node(nodes, edges, item["dataset"], source_type="sql_parse")
        dms_item = dms_by_dataset.get(item["dataset"], {})
        dms_exact = (dms_item.get("exact_records") or [{}])[0] if dms_item.get("exact_records") else {}
        nodes[dataset_id] = node(
            dataset_id,
            ["Dataset"],
            name=item["dataset"],
            layer=item.get("layer", ""),
            source_type="sql_parse",
            comment=clean_html_text(dms_exact.get("comment") or dms_exact.get("description", "")),
            qualified_name=clean_html_text(dms_exact.get("qualifiedName", "")),
            guid=clean_html_text(dms_exact.get("guid", "")),
            db_name=clean_html_text(dms_exact.get("dbName", "")),
            type_name=clean_html_text(dms_exact.get("typeName", "")),
            owner=clean_html_text(dms_exact.get("owner", "")),
            dms_exact_count=dms_item.get("exact_count"),
            dms_total=dms_item.get("total"),
        )
        layer_id = f"layer:{item.get('layer', 'other')}"
        edges[f"{dataset_id}->BELONGS_TO_LAYER->{layer_id}"] = edge(
            f"{dataset_id}->BELONGS_TO_LAYER->{layer_id}",
            dataset_id,
            layer_id,
            "BELONGS_TO_LAYER",
            inferred_by="dataset_name_rule",
        )

        for owner in owner_ids(dms_exact.get("owner", "")):
            owner_node_id = f"owner:{owner}"
            nodes[owner_node_id] = node(owner_node_id, ["Owner"], name=owner)
            edges[f"{owner_node_id}->OWNS->{dataset_id}"] = edge(
                f"{owner_node_id}->OWNS->{dataset_id}",
                owner_node_id,
                dataset_id,
                "OWNS",
                source_system="szconnector",
                source_type="dms_owner",
            )

        for column in dms_exact.get("refColumns") or []:
            if not isinstance(column, dict) or not column.get("name"):
                continue
            column_name = clean_identifier(column["name"])
            if not column_name:
                continue
            column_id = f"column:{item['dataset']}.{column_name}"
            nodes[column_id] = node(
                column_id,
                ["Column"],
                name=column_name,
                dataset=item["dataset"],
                comment=clean_html_text(column.get("comment", "")),
                source_system="szconnector",
                source_type="dms_ref_columns",
            )
            edges[f"{dataset_id}->HAS_COLUMN->{column_id}"] = edge(
                f"{dataset_id}->HAS_COLUMN->{column_id}",
                dataset_id,
                column_id,
                "HAS_COLUMN",
                source_system="szconnector",
            )

        ind_item = indicators_by_dataset.get(item["dataset"], {})
        for rec in ind_item.get("exact_records") or []:
            metric_key = clean_html_text(rec.get("indexId")) or f"{item['dataset']}:{clean_identifier(rec.get('englishName') or rec.get('chineseName'))}"
            metric_id = f"metric:{metric_key}"
            nodes[metric_id] = node(
                metric_id,
                ["Metric"],
                metric_id=clean_html_text(rec.get("indexId", "")),
                chinese_name=clean_html_text(rec.get("chineseName") or rec.get("abbreviation", "")),
                abbreviation=clean_html_text(rec.get("abbreviation", "")),
                english_name=clean_html_text(rec.get("englishName", "")),
                dataset=item["dataset"],
                index_type=code_value(rec.get("indexType")),
                index_gran=code_value(rec.get("indexGran")),
                release_status=code_value(rec.get("releaseStatus")),
                business_cycle=clean_html_text(rec.get("busiCyc", "")),
                horae_task_id=clean_html_text(rec.get("horaeTaskId", "")),
                version_no=clean_html_text(rec.get("versionNo", "")),
                create_time=clean_html_text(rec.get("createTime", "")),
                update_time=clean_html_text(rec.get("lastUpdateTime", "")),
                source_system="szconnector",
            )
            edges[f"{metric_id}->STORED_IN->{dataset_id}"] = edge(
                f"{metric_id}->STORED_IN->{dataset_id}",
                metric_id,
                dataset_id,
                "STORED_IN",
                source_system="szconnector",
            )
            task_id = str(rec.get("horaeTaskId") or "")
            if task_id:
                task_node_id = f"task:{task_id}"
                if task_node_id in nodes:
                    edges[f"{metric_id}->COMPUTED_BY->{task_node_id}"] = edge(
                        f"{metric_id}->COMPUTED_BY->{task_node_id}",
                        metric_id,
                        task_node_id,
                        "COMPUTED_BY",
                        source_system="szconnector",
                    )

            definition = clean_html_text(rec.get("businessDefinition") or "")
            definition_id = f"metric_definition:{metric_key}"
            nodes[definition_id] = node(
                definition_id,
                ["MetricDefinition"],
                metric_id=clean_html_text(rec.get("indexId", "")),
                definition=definition,
                source_system="szconnector",
                source_type="indicator_registry",
                priority="registry",
            )
            edges[f"{metric_id}->HAS_DEFINITION->{definition_id}"] = edge(
                f"{metric_id}->HAS_DEFINITION->{definition_id}",
                metric_id,
                definition_id,
                "HAS_DEFINITION",
                source_system="szconnector",
            )

            for owner in (
                person_names(rec.get("designer"))
                + person_names(rec.get("techDirector"))
                + person_names(rec.get("developer"))
            ):
                owner_node_id = f"owner:{owner}"
                nodes[owner_node_id] = node(owner_node_id, ["Owner"], name=owner)
                edges[f"{owner_node_id}->OWNS->{metric_id}"] = edge(
                    f"{owner_node_id}->OWNS->{metric_id}",
                    owner_node_id,
                    metric_id,
                    "OWNS",
                    source_system="szconnector",
                )

    for item in statements:
        sid = item["statement_id"]
        statement_id = f"sql:{sid}"
        nodes[statement_id] = node(
            statement_id,
            ["SqlStatement"],
            statement_id=sid,
            task_id=item.get("task_id"),
            statement_path=item.get("statement_path"),
            source_log_path=item.get("source_log_path"),
            parse_ok=item.get("parse_ok"),
            extraction_method=item.get("extraction_method"),
            read_dataset_count=item.get("read_dataset_count"),
            write_dataset_count=item.get("write_dataset_count"),
        )
        task_id = f"task:{item.get('task_id')}"
        edges[f"{task_id}->EMITS_SQL->{statement_id}"] = edge(
            f"{task_id}->EMITS_SQL->{statement_id}",
            task_id,
            statement_id,
            "EMITS_SQL",
            source_type=item.get("source_type"),
        )

    for item in dataset_edges:
        statement_id = f"sql:{item['task_id']}_{item.get('to', '').split('_')[-1]}"
        if item["relation"] == "READ_BY":
            dataset_id = f"dataset:{item['from']}"
            sql_id = f"sql:{item['to']}"
            edge_id = f"{sql_id}->READS->{dataset_id}"
            edges[edge_id] = edge(
                edge_id,
                sql_id,
                dataset_id,
                "READS",
                task_id=item.get("task_id"),
                source_type=item.get("source_type"),
            )
        elif item["relation"] == "WRITES":
            sql_id = f"sql:{item['from']}"
            dataset_id = f"dataset:{item['to']}"
            edge_id = f"{sql_id}->WRITES->{dataset_id}"
            edges[edge_id] = edge(
                edge_id,
                sql_id,
                dataset_id,
                "WRITES",
                task_id=item.get("task_id"),
                source_type=item.get("source_type"),
            )

    reads_by_sql: dict[str, set[str]] = {}
    writes_by_sql: dict[str, set[str]] = {}
    for item in dataset_edges:
        if item["relation"] == "READ_BY":
            reads_by_sql.setdefault(item["to"], set()).add(f"dataset:{item['from']}")
        elif item["relation"] == "WRITES":
            writes_by_sql.setdefault(item["from"], set()).add(f"dataset:{item['to']}")

    for sql_id, target_datasets in writes_by_sql.items():
        for target_dataset_id in target_datasets:
            for source_dataset_id in reads_by_sql.get(sql_id, set()):
                edge_id = f"{target_dataset_id}->DATASET_DEPENDS_ON->{source_dataset_id}:{sql_id}"
                edges[edge_id] = edge(
                    edge_id,
                    target_dataset_id,
                    source_dataset_id,
                    "DATASET_DEPENDS_ON",
                    statement_id=f"sql:{sql_id}",
                    source_system="sqlglot",
                    source_type="sql_read_write",
                )

    task_outputs: dict[str, set[str]] = {}
    task_upstreams: dict[str, set[str]] = {}
    for graph_edge in edges.values():
        if graph_edge["type"] == "PRODUCES":
            task_outputs.setdefault(graph_edge["from"], set()).add(graph_edge["to"])
        elif graph_edge["type"] == "DEPENDS_ON":
            task_upstreams.setdefault(graph_edge["from"], set()).add(graph_edge["to"])

    for task_id, upstream_tasks in task_upstreams.items():
        for target_dataset_id in task_outputs.get(task_id, set()):
            for upstream_task_id in upstream_tasks:
                for source_dataset_id in task_outputs.get(upstream_task_id, set()):
                    edge_id = f"{target_dataset_id}->DATASET_DEPENDS_ON->{source_dataset_id}:{task_id}:{upstream_task_id}"
                    edges[edge_id] = edge(
                        edge_id,
                        target_dataset_id,
                        source_dataset_id,
                        "DATASET_DEPENDS_ON",
                        task_id=task_id.removeprefix("task:"),
                        upstream_task_id=upstream_task_id.removeprefix("task:"),
                        source_system="horae",
                        source_type="schedule_dependency_outputs",
                    )

    for item in column_lineage:
        source_dataset = (item.get("source_dataset") or "").lower()
        target_dataset = (item.get("target_dataset") or "").lower()
        source_column = item.get("source_column") or ""
        target_column = item.get("target_column") or ""
        generation_type = item.get("generation_type") or ""
        expression_sql = item.get("expression_sql") or ""
        if generation_type and target_dataset and target_column:
            target_column_id = add_column_node(nodes, edges, target_dataset, target_column)
            statement_raw_id = item.get("statement_id")
            statement_id = f"sql:{statement_raw_id}"
            expr_id = generated_expression_id(str(statement_raw_id), item.get("projection_ordinal"), expression_sql)
            nodes[expr_id] = node(
                expr_id,
                ["GeneratedExpression"],
                expression_id=expr_id,
                generation_type=generation_type,
                expression_sql=expression_sql,
                statement_id=statement_id,
                task_id=item.get("task_id"),
                projection_ordinal=item.get("projection_ordinal"),
                source_system=item.get("source_system"),
                source_type=item.get("source_type"),
            )
            edge_id = (
                f"{target_column_id}->GENERATED_BY_EXPRESSION->{expr_id}:"
                f"{statement_raw_id}:{item.get('branch_ordinal')}:{item.get('projection_ordinal')}"
            )
            edges[edge_id] = edge(
                edge_id,
                target_column_id,
                expr_id,
                "GENERATED_BY_EXPRESSION",
                statement_id=statement_id,
                task_id=item.get("task_id"),
                generation_type=generation_type,
                target_resolution=item.get("target_resolution"),
                branch_ordinal=item.get("branch_ordinal"),
                projection_ordinal=item.get("projection_ordinal"),
                source_system=item.get("source_system"),
                source_type=item.get("source_type"),
            )
            continue
        if not source_dataset or not target_dataset or not source_column or not target_column:
            continue
        source_column_id = add_column_node(nodes, edges, source_dataset, source_column)
        target_column_id = add_column_node(nodes, edges, target_dataset, target_column)
        statement_id = f"sql:{item.get('statement_id')}"
        edge_id = (
            f"{target_column_id}->DERIVED_FROM->{source_column_id}:"
            f"{item.get('statement_id')}:{item.get('branch_ordinal')}:{item.get('projection_ordinal')}"
        )
        edges[edge_id] = edge(
            edge_id,
            target_column_id,
            source_column_id,
            "DERIVED_FROM",
            statement_id=statement_id,
            task_id=item.get("task_id"),
            source_resolution=item.get("source_resolution"),
            target_resolution=item.get("target_resolution"),
            branch_ordinal=item.get("branch_ordinal"),
            projection_ordinal=item.get("projection_ordinal"),
            source_system=item.get("source_system"),
            source_type=item.get("source_type"),
        )

    for item in column_influence:
        source_dataset = (item.get("source_dataset") or "").lower()
        target_dataset = (item.get("target_dataset") or "").lower()
        source_column = item.get("source_column") or ""
        target_column = item.get("target_column") or ""
        if not source_dataset or not target_dataset or not source_column or not target_column:
            continue
        source_column_id = add_column_node(nodes, edges, source_dataset, source_column)
        target_column_id = add_column_node(nodes, edges, target_dataset, target_column)
        statement_id = f"sql:{item.get('statement_id')}"
        edge_id = (
            f"{target_column_id}->INFLUENCED_BY->{source_column_id}:"
            f"{item.get('statement_id')}:{item.get('branch_ordinal')}:{item.get('influence_type')}"
        )
        edges[edge_id] = edge(
            edge_id,
            target_column_id,
            source_column_id,
            "INFLUENCED_BY",
            statement_id=statement_id,
            task_id=item.get("task_id"),
            source_resolution=item.get("source_resolution"),
            target_resolution=item.get("target_resolution"),
            branch_ordinal=item.get("branch_ordinal"),
            influence_type=item.get("influence_type"),
            expression_sql=item.get("expression_sql"),
            source_system=item.get("source_system"),
            source_type=item.get("source_type"),
        )

    metric_storage: dict[str, set[str]] = {}
    metric_compute: dict[str, set[str]] = {}
    dataset_producers: dict[str, set[str]] = {}
    for graph_edge in edges.values():
        if graph_edge["type"] == "STORED_IN":
            metric_storage.setdefault(graph_edge["from"], set()).add(graph_edge["to"])
        elif graph_edge["type"] == "COMPUTED_BY":
            metric_compute.setdefault(graph_edge["from"], set()).add(graph_edge["to"])
        elif graph_edge["type"] == "PRODUCES":
            dataset_producers.setdefault(graph_edge["to"], set()).add(graph_edge["from"])

    for metric_id, dataset_ids in metric_storage.items():
        if metric_compute.get(metric_id):
            continue
        for dataset_id in dataset_ids:
            for task_node_id in dataset_producers.get(dataset_id, set()):
                edge_id = f"{metric_id}->COMPUTED_BY->{task_node_id}"
                edges[edge_id] = edge(
                    edge_id,
                    metric_id,
                    task_node_id,
                    "COMPUTED_BY",
                    source_system="graph_inference",
                    source_type="metric_storage_dataset_producer",
                    inferred_by="metric_stored_dataset_producer",
                )

    decorate_facts(
        nodes,
        edges,
        build_id=build_id,
        project_key=lineage_project_key,
        prefix=args.prefix,
        built_at=now,
    )

    nodes_path = base / f"{args.prefix}_graph_nodes.jsonl"
    edges_path = base / f"{args.prefix}_graph_edges.jsonl"
    nodes_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in nodes.values()) + "\n")
    edges_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in edges.values()) + "\n")

    summary = {
        "nodes_path": str(nodes_path),
        "edges_path": str(edges_path),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_label_distribution": {},
        "edge_type_distribution": {},
        "node_confidence_distribution": {},
        "edge_confidence_distribution": {},
        "build_id": build_id,
        "built_at": now,
    }
    for item in nodes.values():
        for label in item["labels"]:
            summary["node_label_distribution"][label] = summary["node_label_distribution"].get(label, 0) + 1
        confidence = item.get("properties", {}).get("confidence", "unknown")
        summary["node_confidence_distribution"][confidence] = summary["node_confidence_distribution"].get(confidence, 0) + 1
    for item in edges.values():
        summary["edge_type_distribution"][item["type"]] = summary["edge_type_distribution"].get(item["type"], 0) + 1
        confidence = item.get("properties", {}).get("confidence", "unknown")
        summary["edge_confidence_distribution"][confidence] = summary["edge_confidence_distribution"].get(confidence, 0) + 1
    (base / f"{args.prefix}_graph_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
