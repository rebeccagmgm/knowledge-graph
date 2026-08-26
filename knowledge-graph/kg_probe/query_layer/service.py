from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .contracts import CONFIDENCE_RANK, compact_properties, encode_cursor, response, validate_common, warning


TYPE_TO_LABEL = {
    "project": "Project",
    "schedule_task": "ScheduleTask",
    "task": "ScheduleTask",
    "sql_statement": "SqlStatement",
    "dataset": "Dataset",
    "column": "Column",
    "metric": "Metric",
    "metric_definition": "MetricDefinition",
    "code_definition": "CodeDefinition",
    "definition_comparison": "DefinitionComparison",
    "owner": "Owner",
    "data_layer": "DataLayer",
}

LABEL_TO_TYPE = {label: entity_type for entity_type, label in TYPE_TO_LABEL.items() if entity_type != "task"}
LABEL_PRIORITY = [
    "Metric", "ScheduleTask", "Dataset", "Column", "SqlStatement", "MetricDefinition",
    "CodeDefinition", "DefinitionComparison", "Owner", "DataLayer", "Project",
]

TRACE_RELATIONS = {
    "schedule_task": "DEPENDS_ON",
    "dataset": "DATASET_DEPENDS_ON",
    "column": "DERIVED_FROM|INFLUENCED_BY",
    "metric": "STORED_IN|COMPUTED_BY|DATASET_DEPENDS_ON|DEPENDS_ON",
}

GRAPH_RELATION_PROFILES = {
    "schedule": {"DEPENDS_ON"},
    "dataset_lineage": {"DATASET_DEPENDS_ON", "PRODUCES", "CONSUMES", "READS", "WRITES"},
    "column_lineage": {"HAS_COLUMN", "DERIVED_FROM", "INFLUENCED_BY"},
    "code": {"EMITS_SQL", "READS", "WRITES", "PRODUCES", "CONSUMES"},
    "metric": {"STORED_IN", "COMPUTED_BY", "HAS_DEFINITION", "HAS_CODE_DEFINITION", "HAS_COMPARISON"},
    "lineage": {
        "DEPENDS_ON", "PRODUCES", "CONSUMES", "EMITS_SQL", "READS", "WRITES",
        "DATASET_DEPENDS_ON", "HAS_COLUMN", "DERIVED_FROM", "INFLUENCED_BY",
        "STORED_IN", "COMPUTED_BY",
    },
    "all_safe": {
        "DEPENDS_ON", "PRODUCES", "CONSUMES", "EMITS_SQL", "READS", "WRITES",
        "DATASET_DEPENDS_ON", "HAS_COLUMN", "DERIVED_FROM", "INFLUENCED_BY",
        "STORED_IN", "COMPUTED_BY", "HAS_DEFINITION", "HAS_CODE_DEFINITION", "HAS_COMPARISON",
    },
}
GRAPH_ALLOWED_RELATIONS = set().union(*GRAPH_RELATION_PROFILES.values())

CHANGE_TYPES = {"drop", "rename", "type_change", "logic_change", "stop_production", "schedule_change"}
ISSUE_TYPES = {"conflict", "partially_consistent", "code_evidence_insufficient", "registry_missing", "manual_review_required", "consistent"}
FULLTEXT_SAFE = re.compile(r"[^\w\u4e00-\u9fff]+")


def _node_type(node: dict) -> str:
    labels = node.get("labels", [])
    for label in LABEL_PRIORITY:
        if label in labels:
            return LABEL_TO_TYPE.get(label, label.lower())
    return "unknown"


def _display_name(node: dict) -> str:
    props = node.get("properties", {})
    task_name = str(props.get("task_name") or "").strip()
    if task_name and re.fullmatch(r"[0-9,，\s]+", task_name):
        task_name = f"任务 {props.get('task_id')}"
    return str(
        props.get("chinese_name")
        or task_name
        or props.get("name")
        or props.get("definition")
        or props.get("metric_id")
        or props.get("task_id")
        or node.get("id")
        or ""
    )


def entity_ref(node: dict, include_properties: bool = True) -> dict:
    props = node.get("properties", {})
    entity_type = _node_type(node)
    key = (
        props.get("metric_id") or props.get("task_id") or props.get("name")
        or props.get("statement_id") or props.get("definition_id") or node.get("id")
    )
    return {
        "entity_id": node.get("id"),
        "entity_type": entity_type,
        "key": str(key or ""),
        "display_name": _display_name(node),
        "properties": compact_properties(props, include_properties),
    }


def _dedupe_entities(entities: list[dict]) -> list[dict]:
    result = {}
    for item in entities:
        if item.get("entity_id"):
            result[item["entity_id"]] = item
    return list(result.values())


def _confidence_min(edges: list[dict]) -> str:
    values = [edge.get("properties", {}).get("confidence", "medium") for edge in edges]
    if not values:
        return "medium"
    return min(values, key=lambda value: CONFIDENCE_RANK.get(str(value).lower(), 2))


class QueryService:
    primitive_names = {
        "search_entities", "resolve_entity", "get_metric_context", "get_task_context",
        "get_dataset_context", "get_column_context", "trace_upstream", "trace_downstream",
        "analyze_impact", "compare_metric_definitions", "find_definition_issues",
        "explain_lineage_path", "get_recent_changes", "get_graph_neighborhood",
    }

    def __init__(self, store, project_id: str = "trial_project", project_dir: str | Path | None = None):
        self.store = store
        self.project_id = project_id
        self.project_dir = Path(project_dir) if project_dir else None

    def execute(self, primitive: str, payload: dict | None = None) -> dict:
        started = time.perf_counter()
        req = validate_common(payload or {})
        req.setdefault("project_id", self.project_id)
        try:
            if primitive not in self.primitive_names:
                raise ValueError(f"Unknown primitive: {primitive}")
            result = getattr(self, primitive)(req)
        except ValueError as exc:
            result = response(primitive, status="error", answer=str(exc), warnings=[warning("INVALID_REQUEST", str(exc), "error")])
        except Exception as exc:  # noqa: BLE001
            result = response(primitive, status="error", answer="查询执行失败。", warnings=[warning("QUERY_EXECUTION_FAILED", str(exc), "error")])
        result["diagnostics"].setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000, 2))
        result["diagnostics"].setdefault("query_primitive", primitive)
        if not result.get("graph_context"):
            try:
                result["graph_context"] = self.get_graph_status(req.get("project_id"))
            except Exception:  # noqa: BLE001
                result["warnings"].append(warning("GRAPH_STATUS_UNAVAILABLE", "无法读取图谱版本信息。"))
        return result

    def get_graph_status(self, project_id: str | None = None) -> dict:
        project_id = project_id or self.project_id
        rows = self.store.query(
            """
            MATCH (p:Project {project_id: $project_id})
            OPTIONAL MATCH (n:KGNode {project_key: $project_id})
            WITH p, n ORDER BY n.built_at DESC
            WITH p, collect(n)[0] AS latest
            RETURN p, latest
            """,
            {"project_id": project_id},
        )
        if not rows:
            return {"project_id": project_id, "is_latest": False}
        project = rows[0].get("p") or {}
        latest = rows[0].get("latest") or project
        pprops = project.get("properties", {})
        lprops = latest.get("properties", {})
        context = {
            "project_id": project_id,
            "graph_prefix": lprops.get("graph_prefix") or pprops.get("graph_prefix"),
            "build_id": lprops.get("build_id") or pprops.get("build_id"),
            "built_at": lprops.get("built_at") or pprops.get("built_at"),
            "is_latest": True,
            "has_pending_change": False,
        }
        if self.project_dir:
            state_path = self.project_dir / "incremental" / "state.json"
            if state_path.exists():
                state = json.loads(state_path.read_text())
                context["last_scan_at"] = state.get("last_scan_at")
                context["has_pending_change"] = bool(state.get("last_semantic_change"))
        return context

    def _search(self, query: str, entity_types: list[str], project_id: str, limit: int) -> list[dict]:
        labels = []
        for entity_type in entity_types:
            label = TYPE_TO_LABEL.get(entity_type)
            if not label:
                raise ValueError(f"Unsupported entity type: {entity_type}")
            labels.append(label)
        q = query.strip().lower()
        if not q:
            raise ValueError("query is required")
        exact_rows = self.store.query(
            """
            MATCH (n:KGNode {project_key: $project_id})
            WHERE any(label IN labels(n) WHERE label IN $labels)
              AND (
                toLower(n.id) = $query OR
                toLower(coalesce(toString(n.original_id), '')) = $query OR
                toLower(coalesce(toString(n.name), '')) = $query OR
                toLower(coalesce(toString(n.task_id), '')) = $query OR
                toLower(coalesce(toString(n.metric_id), '')) = $query OR
                toLower(coalesce(toString(n.chinese_name), '')) = $query OR
                toLower(coalesce(toString(n.english_name), '')) = $query OR
                toLower(coalesce(toString(n.task_name), '')) = $query
              )
            RETURN n, 1.0 AS score, 'exact' AS match_method
            ORDER BY n.id
            LIMIT $limit
            """,
            {"project_id": project_id, "labels": labels, "query": q, "limit": limit},
        )
        if exact_rows:
            return exact_rows
        fulltext_query = " ".join(token for token in FULLTEXT_SAFE.split(query.strip()) if token)
        if fulltext_query:
            try:
                rows = self.store.query(
                    """
                    CALL db.index.fulltext.queryNodes('kg_entity_search', $fulltext_query) YIELD node, score
                    WHERE node:KGNode
                      AND node.project_key = $project_id
                      AND any(label IN labels(node) WHERE label IN $labels)
                    WITH node, score AS raw_score
                    WITH node, raw_score, CASE
                      WHEN raw_score >= 20 THEN 0.9
                      WHEN raw_score >= 10 THEN 0.8
                      WHEN raw_score >= 5 THEN 0.7
                      ELSE 0.6 END AS normalized_score
                    RETURN node AS n, normalized_score AS score, raw_score, 'fulltext' AS match_method
                    ORDER BY score DESC, raw_score DESC, node.id
                    LIMIT $limit
                    """,
                    {
                        "project_id": project_id,
                        "labels": labels,
                        "fulltext_query": fulltext_query,
                        "limit": limit,
                    },
                )
                if rows:
                    return rows
            except Exception:
                pass
        rows = self.store.query(
            """
            MATCH (n:KGNode {project_key: $project_id})
            WHERE any(label IN labels(n) WHERE label IN $labels)
              AND (
                toLower(n.id) CONTAINS $query OR
                toLower(coalesce(toString(n.name), '')) CONTAINS $query OR
                toLower(coalesce(toString(n.task_id), '')) CONTAINS $query OR
                toLower(coalesce(toString(n.task_name), '')) CONTAINS $query OR
                toLower(coalesce(toString(n.metric_id), '')) CONTAINS $query OR
                toLower(coalesce(toString(n.chinese_name), '')) CONTAINS $query OR
                toLower(coalesce(toString(n.english_name), '')) CONTAINS $query OR
                toLower(coalesce(toString(n.definition), '')) CONTAINS $query OR
                toLower(coalesce(toString(n.comment), '')) CONTAINS $query
              )
            WITH n, CASE
              WHEN toLower(n.id) = $query OR toLower(coalesce(toString(n.name), '')) = $query
                OR toLower(coalesce(toString(n.task_id), '')) = $query
                OR toLower(coalesce(toString(n.metric_id), '')) = $query THEN 1.0
              WHEN toLower(coalesce(toString(n.chinese_name), '')) = $query
                OR toLower(coalesce(toString(n.english_name), '')) = $query
                OR toLower(coalesce(toString(n.task_name), '')) = $query THEN 0.95
              WHEN toLower(coalesce(toString(n.name), '')) STARTS WITH $query
                OR toLower(coalesce(toString(n.chinese_name), '')) STARTS WITH $query THEN 0.8
              ELSE 0.6 END AS score
            WITH n, score, CASE
              WHEN score >= 0.95 THEN 'exact'
              WHEN score >= 0.8 THEN 'prefix'
              ELSE 'contains' END AS match_method
            RETURN n, score, match_method
            ORDER BY score DESC, n.id
            LIMIT $limit
            """,
            {"project_id": project_id, "labels": labels, "query": q, "limit": limit},
        )
        return rows

    def search_entities(self, req: dict) -> dict:
        query = str(req.get("query", ""))
        entity_types = req.get("entity_types") or ["metric", "schedule_task", "dataset", "column"]
        rows = self._search(query, entity_types, req["project_id"], req["limit"] + 1)
        has_more = len(rows) > req["limit"]
        rows = rows[: req["limit"]]
        entities = []
        matches = []
        for row in rows:
            item = entity_ref(row["n"], req["include_properties"])
            item["match"] = {
                "score": row["score"],
                "method": row.get("match_method") or ("exact" if row["score"] >= 0.95 else "fuzzy"),
            }
            if row.get("raw_score") is not None:
                item["match"]["raw_score"] = row["raw_score"]
            entities.append(item)
            matches.append({"entity_id": item["entity_id"], **item["match"]})
        if not entities:
            status, answer, warns = "not_found", f"未找到与“{query}”匹配的实体。", []
        elif len(entities) > 1 and rows[0]["score"] == rows[1]["score"]:
            status, answer = "ambiguous", f"找到{len(entities)}个候选实体，需要进一步消歧。"
            warns = [warning("ENTITY_AMBIGUOUS", "多个候选实体的匹配分数相同。", related_entity_ids=[x["entity_id"] for x in entities])]
        else:
            status, answer, warns = "ok", f"找到{len(entities)}个候选实体。", []
        return response(
            "search_entities", status=status, answer=answer,
            data={"query": query, "matches": matches}, entities=entities, warnings=warns,
            page={"limit": req["limit"], "returned": len(entities), "next_cursor": None, "has_more": has_more},
        )

    def _get_node(self, entity_id: str, project_id: str) -> dict | None:
        rows = self.store.query(
            "MATCH (n:KGNode {id: $entity_id, project_key: $project_id}) RETURN n LIMIT 1",
            {"entity_id": entity_id, "project_id": project_id},
        )
        if rows:
            return rows[0]["n"]
        namespaced_id = f"{project_id}::{entity_id}"
        rows = self.store.query(
            "MATCH (n:KGNode {id: $entity_id, project_key: $project_id}) RETURN n LIMIT 1",
            {"entity_id": namespaced_id, "project_id": project_id},
        )
        return rows[0]["n"] if rows else None

    def _resolve_subject(self, req: dict, expected_types: set[str] | None = None) -> tuple[dict | None, list[dict]]:
        subject = req.get("subject") or {}
        entity_id = subject.get("entity_id") or req.get("entity_id")
        if entity_id:
            node = self._get_node(str(entity_id), req["project_id"])
            if node and (not expected_types or _node_type(node) in expected_types):
                return node, []
            return None, []
        query = subject.get("key") or req.get("query") or req.get("metric_id") or req.get("task_id") or req.get("dataset") or req.get("column")
        entity_type = subject.get("entity_type") or req.get("entity_type")
        types = [entity_type] if entity_type else sorted(expected_types or {"metric", "schedule_task", "dataset", "column"})
        rows = self._search(str(query or ""), types, req["project_id"], 10)
        if not rows:
            return None, []
        exact = [row for row in rows if row["score"] >= 0.95]
        if len(exact) == 1:
            return exact[0]["n"], []
        if len(rows) == 1:
            return rows[0]["n"], []
        return None, self._enrich_candidate_refs([row["n"] for row in rows], req)

    def resolve_entity(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req)
        if node:
            entity = entity_ref(node, req["include_properties"])
            context = self._disambiguation_context(node, req["project_id"])
            if context:
                entity["disambiguation_context"] = context
            return response(
                "resolve_entity",
                answer=f"已解析为{entity['display_name']}。",
                data={"resolved_entity_id": entity["entity_id"], "context": context},
                entities=[entity],
            )
        if candidates:
            needed = self._clarification_fields(candidates)
            return response(
                "resolve_entity",
                status="ambiguous",
                answer="输入对应多个候选实体，需要补充上下文后再执行后续查询。",
                data={
                    "candidate_count": len(candidates),
                    "clarification": {
                        "needed_context": needed,
                        "question": self._clarification_question(needed),
                    },
                },
                entities=candidates,
                warnings=[warning("ENTITY_AMBIGUOUS", "请补充实体类型、所属表、任务 ID 或指标 ID 等上下文。")],
            )
        return response("resolve_entity", status="not_found", answer="未找到目标实体。")

    def _disambiguation_context(self, node: dict, project_id: str) -> dict:
        entity_type = _node_type(node)
        if entity_type == "column":
            rows = self.store.query(
                """
                MATCH (d:Dataset {project_key: $project_id})-[:HAS_COLUMN]->(c:Column {id: $id})
                OPTIONAL MATCH (producer:ScheduleTask {project_key: $project_id})-[:PRODUCES]->(d)
                OPTIONAL MATCH (metric:Metric {project_key: $project_id})-[:STORED_IN]->(d)
                RETURN d, collect(DISTINCT producer.task_id)[0..5] AS producer_task_ids,
                       collect(DISTINCT metric.metric_id)[0..5] AS metric_ids
                LIMIT 1
                """,
                {"project_id": project_id, "id": node["id"]},
            )
            if not rows:
                return {}
            dataset = rows[0].get("d") or {}
            props = dataset.get("properties", {})
            return {
                "dataset_id": dataset.get("id"),
                "dataset_name": props.get("name"),
                "dataset_layer": props.get("layer"),
                "producer_task_ids": [x for x in rows[0].get("producer_task_ids", []) if x],
                "metric_ids": [x for x in rows[0].get("metric_ids", []) if x],
            }
        if entity_type == "dataset":
            rows = self.store.query(
                """
                MATCH (d:Dataset {id: $id, project_key: $project_id})
                OPTIONAL MATCH (producer:ScheduleTask {project_key: $project_id})-[:PRODUCES]->(d)
                OPTIONAL MATCH (consumer:ScheduleTask {project_key: $project_id})-[:CONSUMES]->(d)
                OPTIONAL MATCH (metric:Metric {project_key: $project_id})-[:STORED_IN]->(d)
                RETURN collect(DISTINCT producer.task_id)[0..5] AS producer_task_ids,
                       collect(DISTINCT consumer.task_id)[0..5] AS consumer_task_ids,
                       collect(DISTINCT metric.metric_id)[0..5] AS metric_ids
                """,
                {"project_id": project_id, "id": node["id"]},
            )
            return rows[0] if rows else {}
        if entity_type == "schedule_task":
            props = node.get("properties", {})
            return {
                "task_id": props.get("task_id"),
                "task_type": props.get("task_type"),
                "task_name": props.get("task_name"),
                "layer": props.get("layer"),
                "owner": props.get("owner") or props.get("owners"),
            }
        if entity_type == "metric":
            props = node.get("properties", {})
            return {
                "metric_id": props.get("metric_id"),
                "chinese_name": props.get("chinese_name"),
                "english_name": props.get("english_name"),
                "dataset": props.get("dataset"),
            }
        return {}

    def _enrich_candidate_refs(self, nodes: list[dict], req: dict) -> list[dict]:
        result = []
        for node in nodes:
            item = entity_ref(node, req["include_properties"])
            context = self._disambiguation_context(node, req["project_id"])
            if context:
                item["disambiguation_context"] = context
            result.append(item)
        return result

    def _clarification_fields(self, candidates: list[dict]) -> list[str]:
        types = {item.get("entity_type") for item in candidates}
        if types == {"column"}:
            return ["dataset_name", "task_id", "metric_id"]
        if types == {"dataset"}:
            return ["dataset_full_name", "layer", "producer_task_id"]
        if types == {"schedule_task"}:
            return ["task_id", "task_name"]
        if types == {"metric"}:
            return ["metric_id", "metric_name", "storage_dataset"]
        return ["entity_type", "dataset_name", "task_id", "metric_id"]

    def _clarification_question(self, fields: list[str]) -> str:
        labels = {
            "dataset_name": "字段所属表",
            "task_id": "任务 ID",
            "metric_id": "指标 ID",
            "dataset_full_name": "完整表名",
            "layer": "数据层级",
            "producer_task_id": "生产任务 ID",
            "task_name": "任务名称",
            "metric_name": "指标名称",
            "storage_dataset": "指标存储表",
            "entity_type": "实体类型",
        }
        readable = "、".join(labels.get(field, field) for field in fields)
        return f"请补充{readable}以消除歧义。"

    def _ambiguity_or_not_found(self, primitive: str, candidates: list[dict]) -> dict:
        if candidates:
            return response(primitive, status="ambiguous", answer="目标实体存在歧义。", entities=candidates, warnings=[warning("ENTITY_AMBIGUOUS", "请补充更精确的实体标识。")])
        return response(primitive, status="not_found", answer="未找到目标实体。")

    def _sql_evidence(self, sql_nodes: list[dict], max_chars: int = 1000) -> list[dict]:
        evidence = []
        for node in sql_nodes:
            props = node.get("properties", {})
            excerpt = ""
            path = props.get("statement_path")
            if path and Path(path).exists():
                excerpt = Path(path).read_text(errors="replace")[:max_chars]
            evidence.append({
                "evidence_id": f"ev:{node.get('id')}",
                "evidence_type": "sql_statement",
                "supports": [],
                "source_entity_id": node.get("id"),
                "task_id": props.get("task_id"),
                "excerpt": excerpt,
                "source_type": props.get("source_type") or props.get("extraction_method"),
                "derivation": props.get("extraction_method"),
                "confidence": props.get("confidence", "medium"),
                "build_id": props.get("build_id"),
            })
        return evidence

    def _manual_override(self, metric_id: str) -> dict | None:
        if not self.project_dir:
            return None
        path = self.project_dir / "manual_metric_overrides.json"
        if not path.exists():
            return None
        for item in json.loads(path.read_text()):
            if str(item.get("metric_id")) == metric_id:
                return item
        return None

    def _local_asset_summary(self, asset_type: str, key: str) -> dict | None:
        if not self.project_dir or not key:
            return None
        filename = "task_summaries.jsonl" if asset_type == "task" else "dataset_summaries.jsonl"
        path = self.project_dir / "llm" / filename
        if not path.exists():
            return None
        key_fields = ["task_id", "task_node_id"] if asset_type == "task" else ["dataset", "dataset_id", "name"]
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if any(str(item.get(field, "")) == str(key) for field in key_fields):
                return item
        return None

    def get_metric_context(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req, {"metric"})
        if not node:
            return self._ambiguity_or_not_found("get_metric_context", candidates)
        rows = self.store.query(
            """
            MATCH (m:Metric {id: $id})
            OPTIONAL MATCH (m)-[:STORED_IN]->(d:Dataset)
            OPTIONAL MATCH (m)-[:COMPUTED_BY]->(t:ScheduleTask)
            OPTIONAL MATCH (m)-[:HAS_DEFINITION]->(registered:MetricDefinition)
            OPTIONAL MATCH (m)-[:HAS_CODE_DEFINITION]->(code:CodeDefinition)
            OPTIONAL MATCH (code)-[:HAS_COMPARISON]->(comparison:DefinitionComparison)
            OPTIONAL MATCH (t)-[:EMITS_SQL]->(sql:SqlStatement)
            RETURN m, collect(DISTINCT d) AS datasets, collect(DISTINCT t) AS tasks,
                   collect(DISTINCT registered) AS registered, collect(DISTINCT code) AS code,
                   collect(DISTINCT comparison) AS comparisons, collect(DISTINCT sql)[0..20] AS sql
            """,
            {"id": node["id"]},
        )
        row = rows[0]
        metric_id = str(node.get("properties", {}).get("metric_id", ""))
        manual = self._manual_override(metric_id)
        registered = [x for x in row["registered"] if x]
        code = [x for x in row["code"] if x]
        comparisons = [x for x in row["comparisons"] if x]
        sql_nodes = [x for x in row["sql"] if x]
        compare_status = comparisons[0].get("properties", {}).get("status") if comparisons else None
        warns = []
        status = "ok"
        effective_source = "registered"
        effective_definition = registered[0].get("properties", {}).get("definition") if registered else None
        if manual and not manual.get("needs_review"):
            effective_source = "manual_override"
            effective_definition = manual.get("manual_definition")
        elif code and compare_status not in {"code_evidence_insufficient"}:
            effective_source = "code_definition"
            effective_definition = code[0].get("properties", {}).get("summary")
        if compare_status == "code_evidence_insufficient":
            status = "partial"
            warns.append(warning("EVIDENCE_INSUFFICIENT", "现有代码证据不足以确认完整计算口径。", related_entity_ids=[node["id"]]))
        if compare_status == "conflict":
            warns.append(warning("REGISTERED_DEFINITION_CONFLICT", "代码口径与登记口径存在冲突。", related_entity_ids=[node["id"]]))
        if manual and manual.get("needs_review"):
            warns.append(warning("MANUAL_REVIEW_REQUIRED", "人工补充口径受代码变化影响，需要复核。", related_entity_ids=[node["id"]]))
        entities = [entity_ref(node, req["include_properties"])]
        for key in ["datasets", "tasks", "registered", "code", "comparisons", "sql"]:
            entities.extend(entity_ref(x, req["include_properties"]) for x in row[key] if x)
        data = {
            "metric_id": metric_id,
            "storage_dataset_ids": [x.get("id") for x in row["datasets"] if x],
            "compute_task_ids": [x.get("properties", {}).get("task_id") for x in row["tasks"] if x],
            "registered_definitions": [x.get("properties", {}) for x in registered],
            "code_definitions": [x.get("properties", {}) for x in code],
            "comparisons": [x.get("properties", {}) for x in comparisons],
            "manual_override": manual,
            "effective_definition": {"source": effective_source, "definition": effective_definition},
        }
        return response(
            "get_metric_context", status=status,
            answer=f"指标“{_display_name(node)}”的当前有效口径来源为{effective_source}。",
            data=data, entities=_dedupe_entities(entities),
            evidence=self._sql_evidence(sql_nodes) if req["include_evidence"] else [], warnings=warns,
        )

    def get_task_context(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req, {"schedule_task"})
        if not node:
            return self._ambiguity_or_not_found("get_task_context", candidates)
        rows = self.store.query(
            """
            MATCH (t:ScheduleTask {id: $id})
            OPTIONAL MATCH (t)-[:DEPENDS_ON]->(up:ScheduleTask)
            OPTIONAL MATCH (down:ScheduleTask)-[:DEPENDS_ON]->(t)
            OPTIONAL MATCH (t)-[:PRODUCES]->(produced:Dataset)
            OPTIONAL MATCH (t)-[:CONSUMES]->(consumed:Dataset)
            OPTIONAL MATCH (t)-[:EMITS_SQL]->(sql:SqlStatement)
            OPTIONAL MATCH (metric:Metric)-[:COMPUTED_BY]->(t)
            OPTIONAL MATCH (owner:Owner)-[:OWNS]->(t)
            RETURN t, collect(DISTINCT up) AS upstream, collect(DISTINCT down) AS downstream,
              collect(DISTINCT produced) AS produced, collect(DISTINCT consumed) AS consumed,
              collect(DISTINCT sql)[0..50] AS sql, collect(DISTINCT metric) AS metrics,
              collect(DISTINCT owner) AS owners
            """, {"id": node["id"]},
        )
        row = rows[0]
        entities = [entity_ref(x, req["include_properties"]) for key in row for x in (row[key] if isinstance(row[key], list) else [row[key]]) if isinstance(x, dict) and x.get("id")]
        data = {key: [x.get("id") for x in value if x] for key, value in row.items() if isinstance(value, list)}
        task_id = str(node.get("properties", {}).get("task_id") or "")
        data["summary"] = self._local_asset_summary("task", task_id) or self._local_asset_summary("task", node["id"])
        return response("get_task_context", answer=f"任务{node['properties'].get('task_id')}上下文查询完成。", data=data, entities=_dedupe_entities(entities), evidence=self._sql_evidence([x for x in row["sql"] if x]) if req["include_evidence"] else [])

    def get_dataset_context(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req, {"dataset"})
        if not node:
            return self._ambiguity_or_not_found("get_dataset_context", candidates)
        rows = self.store.query(
            """
            MATCH (d:Dataset {id: $id})
            OPTIONAL MATCH (producer:ScheduleTask)-[:PRODUCES]->(d)
            OPTIONAL MATCH (consumer:ScheduleTask)-[:CONSUMES]->(d)
            OPTIONAL MATCH (d)-[:DATASET_DEPENDS_ON]->(source:Dataset)
            OPTIONAL MATCH (downstream:Dataset)-[:DATASET_DEPENDS_ON]->(d)
            OPTIONAL MATCH (d)-[:HAS_COLUMN]->(column:Column)
            OPTIONAL MATCH (metric:Metric)-[:STORED_IN]->(d)
            OPTIONAL MATCH (owner:Owner)-[:OWNS]->(d)
            RETURN d, collect(DISTINCT producer) AS producers, collect(DISTINCT consumer) AS consumers,
              collect(DISTINCT source) AS sources, collect(DISTINCT downstream) AS downstream,
              collect(DISTINCT column)[0..500] AS columns, collect(DISTINCT metric) AS metrics,
              collect(DISTINCT owner) AS owners
            """, {"id": node["id"]},
        )
        row = rows[0]
        entities = [entity_ref(x, req["include_properties"]) for key in row for x in (row[key] if isinstance(row[key], list) else [row[key]]) if isinstance(x, dict) and x.get("id")]
        data = {key: [x.get("id") for x in value if x] for key, value in row.items() if isinstance(value, list)}
        dataset_name = str(node.get("properties", {}).get("name") or "")
        data["summary"] = self._local_asset_summary("dataset", dataset_name) or self._local_asset_summary("dataset", node["id"])
        return response("get_dataset_context", answer=f"表“{_display_name(node)}”上下文查询完成。", data=data, entities=_dedupe_entities(entities))

    def get_column_context(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req, {"column"})
        if not node:
            return self._ambiguity_or_not_found("get_column_context", candidates)
        rows = self.store.query(
            """
            MATCH (c:Column {id: $id})
            OPTIONAL MATCH (dataset:Dataset)-[:HAS_COLUMN]->(c)
            OPTIONAL MATCH (c)-[up:DERIVED_FROM]->(source:Column)
            OPTIONAL MATCH (downstream:Column)-[down:DERIVED_FROM]->(c)
            RETURN c, dataset, collect(DISTINCT source) AS sources, collect(DISTINCT downstream) AS downstream,
              collect(DISTINCT up) AS upstream_edges, collect(DISTINCT down) AS downstream_edges
            """, {"id": node["id"]},
        )
        row = rows[0]
        entities = [entity_ref(node, req["include_properties"])]
        if row.get("dataset"):
            entities.append(entity_ref(row["dataset"], req["include_properties"]))
        entities.extend(entity_ref(x, req["include_properties"]) for x in row["sources"] + row["downstream"] if x)
        evidence = []
        for edge in row["upstream_edges"] + row["downstream_edges"]:
            if edge:
                evidence.append(self._edge_evidence(edge))
        data = {"source_column_ids": [x.get("id") for x in row["sources"] if x], "downstream_column_ids": [x.get("id") for x in row["downstream"] if x]}
        warns = [] if row["sources"] else [warning("LINEAGE_PARTIAL", "当前没有解析到该字段的上游字段血缘。", related_entity_ids=[node["id"]])]
        status = "ok" if row["sources"] else "partial"
        return response("get_column_context", status=status, answer=f"字段“{_display_name(node)}”上下文查询完成。", data=data, entities=_dedupe_entities(entities), evidence=evidence if req["include_evidence"] else [], warnings=warns)

    def _edge_evidence(self, edge: dict) -> dict:
        props = edge.get("properties", {})
        return {
            "evidence_id": f"ev:{edge.get('id')}",
            "evidence_type": "schedule_relation" if edge.get("type") == "DEPENDS_ON" else "sql_statement",
            "supports": [edge.get("from"), edge.get("to")],
            "source_entity_id": props.get("statement_id") or edge.get("id"),
            "task_id": props.get("task_id"),
            "excerpt": "",
            "source_type": props.get("source_type"),
            "derivation": props.get("source_resolution") or props.get("fact_type"),
            "influence_type": props.get("influence_type"),
            "confidence": props.get("confidence", "medium"),
            "build_id": props.get("build_id"),
        }

    def _path_payload(self, path: dict, direction: str, include_properties: bool) -> tuple[dict, list[dict], list[dict]]:
        nodes = path.get("nodes", [])
        edges = path.get("edges", [])
        raw = json.dumps([node.get("id") for node in nodes] + [edge.get("id") for edge in edges], ensure_ascii=False)
        payload = {
            "path_id": f"path_{hashlib.sha256(raw.encode()).hexdigest()[:16]}",
            "direction": direction,
            "hop_count": len(edges),
            "confidence": _confidence_min(edges),
            "nodes": [node.get("id") for node in nodes],
            "edges": [{
                "type": edge.get("type"),
                "from": edge.get("from"),
                "to": edge.get("to"),
                "confidence": edge.get("properties", {}).get("confidence", "medium"),
                "inferred": edge.get("properties", {}).get("inferred", False),
                "task_id": edge.get("properties", {}).get("task_id"),
                "statement_id": edge.get("properties", {}).get("statement_id"),
                "source_type": edge.get("properties", {}).get("source_type"),
                "influence_type": edge.get("properties", {}).get("influence_type"),
            } for edge in edges],
        }
        entities = [entity_ref(node, include_properties) for node in nodes]
        evidence = [self._edge_evidence(edge) for edge in edges]
        return payload, entities, evidence

    def _trace(self, node: dict, direction: str, req: dict) -> tuple[list[dict], list[dict], list[dict]]:
        entity_type = _node_type(node)
        relations = TRACE_RELATIONS.get(entity_type)
        if not relations:
            raise ValueError(f"Tracing is not supported for {entity_type}")
        hops = req["max_hops"]
        pattern = f"(s:KGNode {{id: $id}})-[:{relations}*1..{hops}]->(n:KGNode)" if direction == "upstream" else f"(s:KGNode {{id: $id}})<-[:{relations}*1..{hops}]-(n:KGNode)"
        fetch_limit = min(req["limit"] * 3, 300)
        rows = self.store.query(f"MATCH p={pattern} RETURN p ORDER BY length(p) LIMIT $limit", {"id": node["id"], "limit": fetch_limit})
        paths, entities, evidence = [], [entity_ref(node, req["include_properties"])], []
        for row in rows:
            payload, path_entities, path_evidence = self._path_payload(row["p"], direction, req["include_properties"])
            threshold = "high" if req["mode"] == "strict" else ("low" if req["mode"] == "exploratory" else req["confidence_min"])
            if CONFIDENCE_RANK.get(payload["confidence"], 2) < CONFIDENCE_RANK[threshold]:
                continue
            paths.append(payload); entities.extend(path_entities); evidence.extend(path_evidence)
            if len(paths) >= req["limit"]:
                break
        return paths, _dedupe_entities(entities), evidence

    def trace_upstream(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req, set(TRACE_RELATIONS))
        if not node:
            return self._ambiguity_or_not_found("trace_upstream", candidates)
        paths, entities, evidence = self._trace(node, "upstream", req)
        status = "ok" if paths else "partial"
        warns = [] if paths else [warning("LINEAGE_PARTIAL", "未找到上游路径。", related_entity_ids=[node["id"]])]
        if any(path["hop_count"] >= req["max_hops"] for path in paths):
            status = "partial"; warns.append(warning("MAX_HOPS_REACHED", "部分路径达到最大遍历深度。"))
        if len(paths) >= req["limit"]:
            status = "partial"; warns.append(warning("RESULT_TRUNCATED", "上游路径数量达到返回上限。"))
        return response("trace_upstream", status=status, answer=f"找到{len(paths)}条上游路径。", data={"subject_id": node["id"], "path_count": len(paths)}, entities=entities, paths=paths, evidence=evidence if req["include_evidence"] else [], warnings=warns, page={"limit": req["limit"], "returned": len(paths), "next_cursor": None, "has_more": len(paths) >= req["limit"]})

    def trace_downstream(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req, set(TRACE_RELATIONS))
        if not node:
            return self._ambiguity_or_not_found("trace_downstream", candidates)
        paths, entities, evidence = self._trace(node, "downstream", req)
        status = "ok" if paths else "partial"
        warns = [] if paths else [warning("LINEAGE_PARTIAL", "未找到下游路径。", related_entity_ids=[node["id"]])]
        if any(path["hop_count"] >= req["max_hops"] for path in paths):
            status = "partial"; warns.append(warning("MAX_HOPS_REACHED", "部分路径达到最大遍历深度。"))
        if len(paths) >= req["limit"]:
            status = "partial"; warns.append(warning("RESULT_TRUNCATED", "下游路径数量达到返回上限。"))
        return response("trace_downstream", status=status, answer=f"找到{len(paths)}条下游路径。", data={"subject_id": node["id"], "path_count": len(paths)}, entities=entities, paths=paths, evidence=evidence if req["include_evidence"] else [], warnings=warns, page={"limit": req["limit"], "returned": len(paths), "next_cursor": None, "has_more": len(paths) >= req["limit"]})

    def _sql_text_matches(self, node: dict, req: dict) -> tuple[list[dict], list[dict]]:
        props = node.get("properties", {})
        needle = str(props.get("name") or props.get("task_id") or "").strip()
        if not needle:
            return [], []
        rows = self.store.query(
            "MATCH (sql:SqlStatement {project_key: $project_id}) RETURN sql LIMIT 5000",
            {"project_id": req["project_id"]},
        )
        entities, evidence = [], []
        pattern = re.compile(rf"(?i)(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])")
        for row in rows:
            sql = row["sql"]; path = sql.get("properties", {}).get("statement_path")
            if not path or not Path(path).exists():
                continue
            text = Path(path).read_text(errors="replace")
            match = pattern.search(text)
            if not match:
                continue
            start = max(match.start() - 300, 0); excerpt = text[start : start + 1000]
            entities.append(entity_ref(sql, req["include_properties"]))
            evidence.append({"evidence_id": f"text:{sql['id']}:{needle}", "evidence_type": "sql_text_fallback", "supports": [node["id"]], "source_entity_id": sql["id"], "task_id": sql.get("properties", {}).get("task_id"), "excerpt": excerpt, "source_type": "sql_text_fallback", "derivation": "exact_token_match", "confidence": "low", "build_id": sql.get("properties", {}).get("build_id")})
            if len(evidence) >= 100:
                break
        return entities, evidence

    def _impact_related_entities(self, paths: list[dict], evidence: list[dict], project_id: str) -> list[dict]:
        column_ids = sorted({entity_id for path in paths for entity_id in path["nodes"] if str(entity_id).startswith("column:")})
        task_ids = sorted({str(item.get("task_id")) for item in evidence if item.get("task_id")})
        related = []
        if column_ids:
            rows = self.store.query(
                """
                MATCH (d:Dataset)-[:HAS_COLUMN]->(c:Column)
                WHERE c.id IN $column_ids
                OPTIONAL MATCH (producer:ScheduleTask)-[:PRODUCES]->(d)
                OPTIONAL MATCH (metric:Metric)-[:STORED_IN]->(d)
                RETURN collect(DISTINCT d) AS datasets, collect(DISTINCT producer) AS producers,
                       collect(DISTINCT metric) AS metrics
                """,
                {"column_ids": column_ids},
            )
            if rows:
                for key in ["datasets", "producers", "metrics"]:
                    related.extend(item for item in rows[0][key] if item)
        if task_ids:
            rows = self.store.query(
                "MATCH (t:ScheduleTask {project_key: $project_id}) WHERE t.task_id IN $task_ids RETURN collect(DISTINCT t) AS tasks",
                {"project_id": project_id, "task_ids": task_ids},
            )
            if rows:
                related.extend(item for item in rows[0]["tasks"] if item)
        return related

    def _impact_sample_paths(self, node: dict, req: dict) -> tuple[list[dict], list[dict], list[dict]]:
        path_limit = min(int(req.get("path_limit", 20)), req["limit"], 100)
        if path_limit <= 0:
            return [], [], []
        sample_req = dict(req)
        sample_req["limit"] = path_limit
        return self._trace(node, "downstream", sample_req)

    def _impact_aggregate(self, node: dict, req: dict) -> tuple[dict, list[dict]]:
        entity_type = _node_type(node)
        params = {
            "id": node["id"],
            "project_id": req["project_id"],
            "hops": req["max_hops"],
            "limit": req["limit"],
        }
        hops = int(req["max_hops"])
        if entity_type == "column":
            rows = self.store.query(
                """
                MATCH (subject:Column {id: $id, project_key: $project_id})
                OPTIONAL MATCH (subject_dataset:Dataset {project_key: $project_id})-[:HAS_COLUMN]->(subject)
                OPTIONAL MATCH path=(subject)<-[:DERIVED_FROM|INFLUENCED_BY*1..__HOPS__]-(down_col:Column {project_key: $project_id})
                WITH subject, collect(DISTINCT subject_dataset) AS subject_datasets, collect(DISTINCT down_col) AS affected_columns
                OPTIONAL MATCH (affected_dataset:Dataset {project_key: $project_id})-[:HAS_COLUMN]->(affected_column:Column)
                WHERE affected_column IN affected_columns
                WITH subject, affected_columns, subject_datasets, collect(DISTINCT affected_dataset) AS downstream_datasets
                WITH subject, affected_columns, subject_datasets + downstream_datasets AS affected_datasets
                OPTIONAL MATCH (producer:ScheduleTask {project_key: $project_id})-[:PRODUCES]->(produced:Dataset)
                WHERE produced IN affected_datasets
                WITH subject, affected_columns, affected_datasets, collect(DISTINCT producer) AS producer_tasks
                OPTIONAL MATCH (consumer:ScheduleTask {project_key: $project_id})-[:CONSUMES]->(consumed:Dataset)
                WHERE consumed IN affected_datasets
                WITH subject, affected_columns, affected_datasets, producer_tasks, collect(DISTINCT consumer) AS consumer_tasks
                OPTIONAL MATCH (sql_consumer:ScheduleTask {project_key: $project_id})-[:EMITS_SQL]->(:SqlStatement)-[:READS]->(sql_consumed:Dataset)
                WHERE sql_consumed IN affected_datasets
                WITH subject, affected_columns, affected_datasets, producer_tasks, consumer_tasks, collect(DISTINCT sql_consumer) AS sql_consumer_tasks
                OPTIONAL MATCH (metric:Metric {project_key: $project_id})-[:STORED_IN]->(metric_dataset:Dataset)
                WHERE metric_dataset IN affected_datasets
                WITH subject, affected_columns, affected_datasets, producer_tasks + consumer_tasks + sql_consumer_tasks AS impact_tasks, collect(DISTINCT metric) AS metrics
                OPTIONAL MATCH (subject)<-[rels:DERIVED_FROM|INFLUENCED_BY*1..__HOPS__]-(:Column {project_key: $project_id})
                UNWIND rels AS rel
                WITH subject, affected_columns, affected_datasets, impact_tasks, metrics,
                     collect(DISTINCT rel.task_id) AS lineage_task_ids
                OPTIONAL MATCH (lineage_task:ScheduleTask {project_key: $project_id})
                WHERE lineage_task.task_id IN lineage_task_ids
                RETURN subject,
                       affected_columns,
                       affected_datasets,
                       impact_tasks AS producer_tasks,
                       collect(DISTINCT lineage_task) AS lineage_tasks,
                       metrics
                """.replace("__HOPS__", str(hops)),
                params,
            )
        elif entity_type == "dataset":
            rows = self.store.query(
                """
                MATCH (subject:Dataset {id: $id, project_key: $project_id})
                OPTIONAL MATCH path=(subject)<-[:DATASET_DEPENDS_ON*1..__HOPS__]-(down_dataset:Dataset {project_key: $project_id})
                WITH subject, collect(DISTINCT down_dataset) AS affected_datasets
                OPTIONAL MATCH (producer:ScheduleTask {project_key: $project_id})-[:PRODUCES]->(produced:Dataset)
                WHERE produced IN affected_datasets
                WITH subject, affected_datasets, collect(DISTINCT producer) AS producer_tasks
                OPTIONAL MATCH (consumer:ScheduleTask {project_key: $project_id})-[:CONSUMES]->(consumed:Dataset)
                WHERE consumed = subject OR consumed IN affected_datasets
                WITH subject, affected_datasets, producer_tasks, collect(DISTINCT consumer) AS consumer_tasks
                OPTIONAL MATCH (sql_consumer:ScheduleTask {project_key: $project_id})-[:EMITS_SQL]->(:SqlStatement)-[:READS]->(sql_consumed:Dataset)
                WHERE sql_consumed = subject OR sql_consumed IN affected_datasets
                WITH subject, affected_datasets, producer_tasks, consumer_tasks, collect(DISTINCT sql_consumer) AS sql_consumer_tasks
                WITH subject, affected_datasets, producer_tasks + consumer_tasks + sql_consumer_tasks AS producer_tasks
                OPTIONAL MATCH (metric:Metric {project_key: $project_id})-[:STORED_IN]->(metric_dataset:Dataset)
                WHERE metric_dataset = subject OR metric_dataset IN affected_datasets
                RETURN subject,
                       [] AS affected_columns,
                       affected_datasets,
                       producer_tasks,
                       [] AS lineage_tasks,
                       collect(DISTINCT metric) AS metrics
                """.replace("__HOPS__", str(hops)),
                params,
            )
        elif entity_type == "schedule_task":
            rows = self.store.query(
                """
                MATCH (subject:ScheduleTask {id: $id, project_key: $project_id})
                OPTIONAL MATCH path=(subject)<-[:DEPENDS_ON*1..__HOPS__]-(down_task:ScheduleTask {project_key: $project_id})
                WITH subject, collect(DISTINCT down_task) AS affected_tasks
                OPTIONAL MATCH (task:ScheduleTask {project_key: $project_id})-[:PRODUCES]->(dataset:Dataset)
                WHERE task = subject OR task IN affected_tasks
                WITH subject, affected_tasks, collect(DISTINCT dataset) AS affected_datasets
                OPTIONAL MATCH (metric:Metric {project_key: $project_id})-[:COMPUTED_BY]->(task_for_metric:ScheduleTask)
                WHERE task_for_metric = subject OR task_for_metric IN affected_tasks
                RETURN subject,
                       [] AS affected_columns,
                       affected_datasets,
                       affected_tasks AS producer_tasks,
                       [] AS lineage_tasks,
                       collect(DISTINCT metric) AS metrics
                """.replace("__HOPS__", str(hops)),
                params,
            )
        else:
            raise ValueError(f"Impact analysis is not supported for {entity_type}")

        if not rows:
            return {
                "summary": {
                    "affected_column_count": 0,
                    "affected_dataset_count": 0,
                    "affected_task_count": 0,
                    "affected_metric_count": 0,
                },
                "affected_columns": [],
                "affected_datasets": [],
                "affected_tasks": [],
                "affected_metrics": [],
            }, []
        row = rows[0]
        affected_columns = [item for item in row.get("affected_columns", []) if item]
        affected_datasets = [item for item in row.get("affected_datasets", []) if item]
        affected_tasks = [item for item in (row.get("producer_tasks", []) + row.get("lineage_tasks", [])) if item]
        affected_metrics = [item for item in row.get("metrics", []) if item]
        entities = []
        for group in [affected_columns, affected_datasets, affected_tasks, affected_metrics]:
            entities.extend(entity_ref(item, req["include_properties"]) for item in group)
        entity_by_id = {item["entity_id"]: item for item in _dedupe_entities(entities)}
        summary = {
            "affected_column_count": len({item.get("id") for item in affected_columns}),
            "affected_dataset_count": len({item.get("id") for item in affected_datasets}),
            "affected_task_count": len({item.get("id") for item in affected_tasks}),
            "affected_metric_count": len({item.get("id") for item in affected_metrics}),
        }
        offset = req["cursor_offset"]
        limit = req["limit"]
        data = {
            "summary": summary,
            "affected_columns": [entity_by_id[entity_id] for entity_id in sorted({item.get("id") for item in affected_columns if item.get("id") in entity_by_id})][offset : offset + limit],
            "affected_datasets": [entity_by_id[entity_id] for entity_id in sorted({item.get("id") for item in affected_datasets if item.get("id") in entity_by_id})][offset : offset + limit],
            "affected_tasks": [entity_by_id[entity_id] for entity_id in sorted({item.get("id") for item in affected_tasks if item.get("id") in entity_by_id})][offset : offset + limit],
            "affected_metrics": [entity_by_id[entity_id] for entity_id in sorted({item.get("id") for item in affected_metrics if item.get("id") in entity_by_id})][offset : offset + limit],
        }
        return data, list(entity_by_id.values())

    def _impact_groups(self, node: dict, req: dict) -> list[dict]:
        entity_type = _node_type(node)
        if entity_type == "column":
            hops = int(req["max_hops"])
            rows = self.store.query(
                """
                MATCH (subject:Column {id: $id, project_key: $project_id})
                OPTIONAL MATCH path=(subject)<-[rels:DERIVED_FROM|INFLUENCED_BY*1..__HOPS__]-(down_col:Column {project_key: $project_id})
                UNWIND rels AS rel
                WITH down_col, rel, CASE
                  WHEN type(rel) = 'DERIVED_FROM' THEN 'direct_value_lineage'
                  ELSE coalesce(rel.influence_type, 'indirect_context_influence') END AS reason
                OPTIONAL MATCH (dataset:Dataset {project_key: $project_id})-[:HAS_COLUMN]->(down_col)
                RETURN reason,
                       count(DISTINCT down_col) AS affected_column_count,
                       count(DISTINCT dataset) AS affected_dataset_count,
                       collect(DISTINCT rel.task_id)[0..20] AS sample_task_ids,
                       collect(DISTINCT down_col.id)[0..20] AS sample_column_ids,
                       collect(DISTINCT rel.statement_id)[0..10] AS sample_statement_ids
                ORDER BY affected_column_count DESC, reason
                """.replace("__HOPS__", str(hops)),
                {"id": node["id"], "project_id": req["project_id"], "hops": req["max_hops"]},
            )
            return [self._impact_group_payload(row) for row in rows if row.get("reason")]
        if entity_type == "dataset":
            return [
                {
                    "group": "dataset_downstream_lineage",
                    "severity": "high",
                    "reason": "表级下游血缘或消费关系受影响。",
                    "affected_dataset_count": 0,
                    "affected_column_count": 0,
                    "affected_task_count": 0,
                    "sample_task_ids": [],
                    "sample_column_ids": [],
                    "sample_statement_ids": [],
                }
            ]
        if entity_type == "schedule_task":
            return [
                {
                    "group": "schedule_downstream_lineage",
                    "severity": "high",
                    "reason": "调度下游任务、产出表和指标可能受影响。",
                    "affected_dataset_count": 0,
                    "affected_column_count": 0,
                    "affected_task_count": 0,
                    "sample_task_ids": [],
                    "sample_column_ids": [],
                    "sample_statement_ids": [],
                }
            ]
        return []

    def _impact_group_payload(self, row: dict) -> dict:
        reason = row.get("reason")
        severity_by_reason = {
            "direct_value_lineage": "high",
            "filter": "medium",
            "join_condition": "medium",
            "join_using": "medium",
            "group_by": "medium",
            "having": "medium",
            "qualify": "medium",
            "order_by": "low",
            "indirect_context_influence": "medium",
        }
        description_by_reason = {
            "direct_value_lineage": "字段作为下游字段值的直接来源，通常需要检查字段删除、改名、类型和口径变更。",
            "filter": "字段参与过滤条件，会影响结果集范围。",
            "join_condition": "字段参与关联条件，会影响数据匹配关系。",
            "join_using": "字段参与 USING 关联，会影响数据匹配关系。",
            "group_by": "字段参与分组，会影响聚合粒度。",
            "having": "字段参与聚合后过滤，会影响结果集范围。",
            "qualify": "字段参与窗口结果过滤，会影响结果集范围。",
            "order_by": "字段参与排序，通常影响排序或窗口相关逻辑。",
            "indirect_context_influence": "字段通过上下文关系间接影响下游。",
        }
        return {
            "group": reason,
            "severity": severity_by_reason.get(reason, "medium"),
            "reason": description_by_reason.get(reason, "字段影响下游逻辑。"),
            "affected_column_count": row.get("affected_column_count", 0),
            "affected_dataset_count": row.get("affected_dataset_count", 0),
            "sample_task_ids": [x for x in row.get("sample_task_ids", []) if x],
            "sample_column_ids": [x for x in row.get("sample_column_ids", []) if x],
            "sample_statement_ids": [x for x in row.get("sample_statement_ids", []) if x],
        }

    def _impact_explanations(self, paths: list[dict], groups: list[dict]) -> list[dict]:
        explanations = []
        for group in groups:
            explanations.append(
                {
                    "group": group["group"],
                    "severity": group["severity"],
                    "explanation": group["reason"],
                    "sample_task_ids": group.get("sample_task_ids", [])[:5],
                    "sample_column_ids": group.get("sample_column_ids", [])[:5],
                    "sample_statement_ids": group.get("sample_statement_ids", [])[:3],
                }
            )
        for path in paths[:5]:
            relation_types = [edge.get("type") for edge in path.get("edges", [])]
            influence_types = [edge.get("influence_type") for edge in path.get("edges", []) if edge.get("influence_type")]
            explanations.append(
                {
                    "group": "sample_path",
                    "severity": path.get("confidence", "medium"),
                    "explanation": "代表路径说明影响如何沿图关系传播。",
                    "path_id": path.get("path_id"),
                    "relation_types": relation_types,
                    "influence_types": influence_types,
                }
            )
        return explanations

    def analyze_impact(self, req: dict) -> dict:
        change_type = req.get("change_type", "logic_change")
        if change_type not in CHANGE_TYPES:
            raise ValueError(f"change_type must be one of {sorted(CHANGE_TYPES)}")
        node, candidates = self._resolve_subject(req, {"schedule_task", "dataset", "column"})
        if not node:
            return self._ambiguity_or_not_found("analyze_impact", candidates)
        aggregate, aggregate_entities = self._impact_aggregate(node, req)
        paths, path_entities, evidence = self._impact_sample_paths(node, req)
        impact_groups = self._impact_groups(node, req)
        entities = [entity_ref(node, req["include_properties"])] + aggregate_entities + path_entities
        warns = []
        include_fallback = bool(req.get("include_sql_fallback")) or req["mode"] == "exploratory"
        text_entities, text_evidence = [], []
        if include_fallback:
            text_entities, text_evidence = self._sql_text_matches(node, req)
            entities.extend(text_entities); evidence.extend(text_evidence)
            if text_evidence:
                warns.append(warning("SQL_FALLBACK_ONLY", "部分结果仅来自SQL文本匹配，需要人工确认。"))
        entity_by_id = {item["entity_id"]: item for item in _dedupe_entities(entities)}
        summary = aggregate["summary"]
        total_affected = sum(summary.values())
        offset = req["cursor_offset"]
        limit = req["limit"]
        bucket_counts = {
            "affected_columns": summary["affected_column_count"],
            "affected_datasets": summary["affected_dataset_count"],
            "affected_tasks": summary["affected_task_count"],
            "affected_metrics": summary["affected_metric_count"],
        }
        has_more = any(offset + limit < count for count in bucket_counts.values())
        returned_count = sum(len(aggregate[key]) for key in bucket_counts)
        data = {
            "subject": entity_ref(node, req["include_properties"]),
            "change_type": change_type,
            **aggregate,
            "impact_groups": impact_groups,
            "impact_explanations": self._impact_explanations(paths, impact_groups),
            "text_match_only": text_entities[: req["limit"]],
            "sample_path_count": len(paths),
        }
        status = "ok" if total_affected else "partial"
        if total_affected and not paths:
            warns.append(warning("SAMPLE_PATH_UNAVAILABLE", "已聚合到受影响实体，但未返回代表路径。"))
        if not total_affected:
            warns.append(warning("LINEAGE_PARTIAL", "未找到确定性下游路径，不能据此断言没有影响。"))
        if has_more:
            status = "partial"; warns.append(warning("RESULT_TRUNCATED", "明细数量达到返回上限；summary 为全量计数，明细为分页样本。"))
        answer = (
            "影响分析完成："
            f"受影响字段{summary['affected_column_count']}个、"
            f"表{summary['affected_dataset_count']}张、"
            f"任务{summary['affected_task_count']}个、"
            f"指标{summary['affected_metric_count']}个。"
        )
        return response(
            "analyze_impact",
            status=status,
            answer=answer,
            data=data,
            entities=list(entity_by_id.values()),
            paths=paths,
            evidence=evidence if req["include_evidence"] else [],
            warnings=warns,
            page={"limit": req["limit"], "returned": returned_count, "next_cursor": encode_cursor(offset + limit) if has_more else None, "has_more": has_more},
        )

    def compare_metric_definitions(self, req: dict) -> dict:
        metric_result = self.get_metric_context(req)
        metric_result["primitive"] = "compare_metric_definitions"
        if metric_result["status"] in {"not_found", "ambiguous", "error"}:
            return metric_result
        data = metric_result["data"]
        comparison = data.get("comparisons", [])
        data["comparison_status"] = comparison[0].get("status") if comparison else "missing"
        metric_result["answer"] = f"口径比较状态为{data['comparison_status']}。"
        return metric_result

    def find_definition_issues(self, req: dict) -> dict:
        issue_types = req.get("issue_types") or ["conflict", "partially_consistent", "code_evidence_insufficient", "registry_missing"]
        invalid = set(issue_types) - ISSUE_TYPES
        if invalid:
            raise ValueError(f"Unsupported issue types: {sorted(invalid)}")
        comparison_types = [item for item in issue_types if item != "manual_review_required"]
        summary_rows = self.store.query(
            """
            MATCH (:Metric {project_key: $project_id})-[:HAS_CODE_DEFINITION]->(:CodeDefinition)-[:HAS_COMPARISON]->(comparison:DefinitionComparison)
            RETURN comparison.status AS status, count(*) AS count
            ORDER BY status
            """,
            {"project_id": req["project_id"]},
        )
        issue_summary = {str(row["status"]): row["count"] for row in summary_rows}
        selected_total = sum(issue_summary.get(item, 0) for item in comparison_types)
        rows = self.store.query(
            """
            MATCH (m:Metric {project_key: $project_id})-[:HAS_CODE_DEFINITION]->(code:CodeDefinition)-[:HAS_COMPARISON]->(comparison:DefinitionComparison)
            WHERE comparison.status IN $issue_types
            OPTIONAL MATCH (m)-[:HAS_DEFINITION]->(registered:MetricDefinition)
            OPTIONAL MATCH (m)-[:STORED_IN]->(dataset:Dataset)
            OPTIONAL MATCH (m)-[:COMPUTED_BY]->(task:ScheduleTask)
            RETURN m, code, comparison, collect(DISTINCT registered) AS registered,
                   collect(DISTINCT dataset) AS datasets, collect(DISTINCT task) AS tasks
            ORDER BY comparison.status, m.metric_id
            SKIP $offset
            LIMIT $limit
            """, {"project_id": req["project_id"], "issue_types": comparison_types, "offset": req["cursor_offset"], "limit": req["limit"]},
        )
        entities, issues = [], []
        for row in rows:
            for key in ["m", "code", "comparison"]:
                entities.append(entity_ref(row[key], req["include_properties"]))
            for item in row["registered"] + row["datasets"] + row["tasks"]:
                if item: entities.append(entity_ref(item, req["include_properties"]))
            metric_props = row["m"].get("properties", {})
            code_props = row["code"].get("properties", {})
            comparison_props = row["comparison"].get("properties", {})
            issues.append({
                "metric_id": metric_props.get("metric_id"),
                "metric_name": _display_name(row["m"]),
                "english_name": metric_props.get("english_name") or metric_props.get("english_name_clean"),
                "status": comparison_props.get("status"),
                "comparison_id": row["comparison"].get("id"),
                "code_definition_id": row["code"].get("id"),
                "code_summary": code_props.get("summary"),
                "registered_definitions": [item.get("properties", {}).get("definition") for item in row["registered"] if item],
                "conflict_points": comparison_props.get("conflict_points"),
                "missing_in_registry": comparison_props.get("missing_in_registry"),
                "insufficient_code_evidence": comparison_props.get("insufficient_code_evidence"),
                "recommended_definition": comparison_props.get("recommended_definition"),
                "storage_dataset_ids": [item.get("id") for item in row["datasets"] if item],
                "compute_task_ids": [item.get("properties", {}).get("task_id") for item in row["tasks"] if item],
            })
        if "manual_review_required" in issue_types and self.project_dir:
            override_path = self.project_dir / "manual_metric_overrides.json"
            overrides = json.loads(override_path.read_text()) if override_path.exists() else []
            metric_ids = [str(item.get("metric_id")) for item in overrides if item.get("needs_review")]
            if metric_ids:
                manual_rows = self.store.query(
                    "MATCH (m:Metric {project_key: $project_id}) WHERE m.metric_id IN $metric_ids RETURN m LIMIT $limit",
                    {"project_id": req["project_id"], "metric_ids": metric_ids, "limit": req["limit"]},
                )
                for row in manual_rows:
                    metric = row["m"]
                    entities.append(entity_ref(metric, req["include_properties"]))
                    issues.append({"metric_id": metric.get("properties", {}).get("metric_id"), "metric_name": _display_name(metric), "status": "manual_review_required", "comparison_id": None})
        answer_total = selected_total if comparison_types else len(issues)
        return response(
            "find_definition_issues",
            answer=f"找到{answer_total}个口径问题，当前返回{len(issues)}个。",
            data={"issue_types": issue_types, "summary": issue_summary, "selected_total": answer_total, "issues": issues},
            entities=_dedupe_entities(entities),
            page={
                "limit": req["limit"],
                "returned": len(issues),
                "next_cursor": encode_cursor(req["cursor_offset"] + len(issues)) if req["cursor_offset"] + len(issues) < answer_total else None,
                "has_more": req["cursor_offset"] + len(issues) < answer_total,
            },
        )

    def get_recent_changes(self, req: dict) -> dict:
        if not self.project_dir:
            return response(
                "get_recent_changes",
                status="partial",
                answer="当前查询服务未配置项目产物目录，无法读取增量变化事件。",
                warnings=[warning("PROJECT_DIR_UNAVAILABLE", "请为查询服务配置 project_dir 或 project_dir_root。")],
            )
        incremental_dir = self.project_dir / "incremental"
        changes_dir = incremental_dir / "changes"
        state_path = incremental_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        files = sorted(changes_dir.glob("*.json"), key=lambda path: path.name, reverse=True) if changes_dir.exists() else []
        offset = req["cursor_offset"]
        limit = req["limit"]
        selected = files[offset : offset + limit]
        events = []
        for path in selected:
            item = json.loads(path.read_text())
            events.append(
                {
                    "event_id": path.stem,
                    "path": str(path),
                    "project_id": item.get("project_id"),
                    "detected_at": item.get("detected_at"),
                    "semantic_change": bool(item.get("semantic_change")),
                    "status": "changed" if item.get("semantic_change") else "unchanged",
                    "added_task_count": len(item.get("added_task_ids", [])),
                    "removed_task_count": len(item.get("removed_task_ids", [])),
                    "metadata_changed_task_count": len(item.get("metadata_changed_task_ids", [])),
                    "code_changed_task_count": len(item.get("code_changed_task_ids", [])),
                    "text_only_changed_task_count": len(item.get("text_only_changed_task_ids", [])),
                    "dependency_edges_added_count": len(item.get("dependency_edges_added", [])),
                    "dependency_edges_removed_count": len(item.get("dependency_edges_removed", [])),
                    "dataset_schema_changed": bool(item.get("dataset_schema_changed")),
                    "indicator_registry_changed": bool(item.get("indicator_registry_changed")),
                    "affected_task_count": len(item.get("affected_task_ids", [])),
                    "affected_metric_count": len(item.get("affected_metric_ids", [])),
                    "affected_task_ids_sample": item.get("affected_task_ids", [])[:20],
                    "affected_metric_ids_sample": item.get("affected_metric_ids", [])[:20],
                    "refresh_quality": item.get("refresh_quality", {}),
                }
            )
        changed_count = sum(1 for path in files if json.loads(path.read_text()).get("semantic_change"))
        data = {
            "state": state,
            "summary": {
                "event_count": len(files),
                "semantic_change_event_count": changed_count,
                "last_scan_at": state.get("last_scan_at"),
                "last_semantic_change": state.get("last_semantic_change"),
                "last_change_path": state.get("last_change_path"),
            },
            "events": events,
        }
        has_more = offset + limit < len(files)
        status = "ok" if events else "partial"
        warns = [] if events else [warning("NO_CHANGE_EVENTS", "未找到增量变化事件文件。")]
        return response(
            "get_recent_changes",
            status=status,
            answer=f"读取到{len(files)}个增量变化事件，当前返回{len(events)}个。",
            data=data,
            warnings=warns,
            page={
                "limit": limit,
                "returned": len(events),
                "next_cursor": encode_cursor(offset + limit) if has_more else None,
                "has_more": has_more,
            },
        )

    def _graph_relation_types(self, req: dict) -> tuple[list[str], str]:
        profile = str(req.get("relation_profile") or "lineage")
        if profile not in GRAPH_RELATION_PROFILES:
            raise ValueError(f"relation_profile must be one of {sorted(GRAPH_RELATION_PROFILES)}")
        relation_types = req.get("edge_types") or sorted(GRAPH_RELATION_PROFILES[profile])
        if isinstance(relation_types, str):
            relation_types = [item.strip() for item in relation_types.split(",") if item.strip()]
        relation_types = [str(item).strip().upper() for item in relation_types]
        unsupported = sorted(set(relation_types) - GRAPH_ALLOWED_RELATIONS)
        if unsupported:
            raise ValueError(f"Unsupported edge_types: {unsupported}")
        allowed_by_profile = GRAPH_RELATION_PROFILES[profile]
        outside_profile = sorted(set(relation_types) - allowed_by_profile)
        if outside_profile and not req.get("allow_custom_edge_types"):
            raise ValueError(f"edge_types are outside relation_profile={profile}: {outside_profile}")
        return sorted(set(relation_types)), profile

    def _visual_node(self, node: dict, depth: int, include_properties: bool) -> dict:
        ref = entity_ref(node, include_properties)
        return {
            "id": ref["entity_id"],
            "type": ref["entity_type"],
            "label": ref["display_name"],
            "key": ref["key"],
            "depth": depth,
            "group": ref["entity_type"],
            "properties": ref["properties"],
        }

    def _visual_edge(self, edge: dict, source: str, target: str) -> dict:
        props = edge.get("properties", {})
        raw = json.dumps([edge.get("id"), source, target, edge.get("type")], ensure_ascii=False)
        return {
            "id": f"ve:{hashlib.sha256(raw.encode()).hexdigest()[:16]}",
            "source": source,
            "target": target,
            "type": edge.get("type"),
            "label": edge.get("type"),
            "graph_source": edge.get("from"),
            "graph_target": edge.get("to"),
            "confidence": props.get("confidence", "medium"),
            "inferred": props.get("inferred", False),
            "task_id": props.get("task_id"),
            "statement_id": props.get("statement_id"),
            "source_type": props.get("source_type"),
            "influence_type": props.get("influence_type"),
        }

    def _neighborhood_step(
        self,
        frontier: set[str],
        relation_types: list[str],
        direction: str,
        project_id: str,
        limit: int,
    ) -> list[dict]:
        if not frontier or limit <= 0:
            return []
        relations = "|".join(relation_types)
        params = {"project_id": project_id, "frontier": sorted(frontier), "limit": limit}
        rows: list[dict] = []
        if direction in {"upstream", "both"}:
            rows.extend(
                self.store.query(
                    f"""
                    MATCH (current:KGNode {{project_key: $project_id}})
                    WHERE current.id IN $frontier
                    MATCH (current)-[r:{relations}]->(neighbor:KGNode {{project_key: $project_id}})
                    RETURN current, neighbor, r, 'out' AS traversal
                    ORDER BY type(r), neighbor.id
                    LIMIT $limit
                    """,
                    params,
                )
            )
        if direction in {"downstream", "both"}:
            rows.extend(
                self.store.query(
                    f"""
                    MATCH (current:KGNode {{project_key: $project_id}})
                    WHERE current.id IN $frontier
                    MATCH (neighbor:KGNode {{project_key: $project_id}})-[r:{relations}]->(current)
                    RETURN current, neighbor, r, 'in' AS traversal
                    ORDER BY type(r), neighbor.id
                    LIMIT $limit
                    """,
                    params,
                )
            )
        deduped = {}
        for row in rows:
            edge = row.get("r") or {}
            current = row.get("current") or {}
            neighbor = row.get("neighbor") or {}
            key = (current.get("id"), neighbor.get("id"), edge.get("id"), row.get("traversal"))
            deduped.setdefault(key, row)
        return list(deduped.values())[:limit]

    def _edge_path_payload(self, edge: dict, source: str, target: str, direction: str) -> dict:
        raw = json.dumps([source, target, edge.get("id"), edge.get("type")], ensure_ascii=False)
        props = edge.get("properties", {})
        return {
            "path_id": f"path_{hashlib.sha256(raw.encode()).hexdigest()[:16]}",
            "direction": direction,
            "hop_count": 1,
            "confidence": props.get("confidence", "medium"),
            "nodes": [source, target],
            "edges": [{
                "type": edge.get("type"),
                "from": edge.get("from"),
                "to": edge.get("to"),
                "confidence": props.get("confidence", "medium"),
                "inferred": props.get("inferred", False),
                "task_id": props.get("task_id"),
                "statement_id": props.get("statement_id"),
                "source_type": props.get("source_type"),
                "influence_type": props.get("influence_type"),
            }],
        }

    def get_graph_neighborhood(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req)
        if not node:
            return self._ambiguity_or_not_found("get_graph_neighborhood", candidates)
        direction = str(req.get("direction") or "downstream").lower()
        if direction not in {"upstream", "downstream", "both"}:
            raise ValueError("direction must be upstream, downstream, or both")
        relation_types, profile = self._graph_relation_types(req)
        hops = min(req["max_hops"], int(req.get("visual_max_hops", 8)))
        limit_nodes = min(max(1, int(req.get("limit_nodes", req["limit"]))), 500)
        limit_edges = min(max(1, int(req.get("limit_edges", limit_nodes * 3))), 1500)
        visual_nodes: dict[str, dict] = {node["id"]: self._visual_node(node, 0, req["include_properties"])}
        visual_edges: dict[tuple[str, str, str, str], dict] = {}
        entities = [entity_ref(node, req["include_properties"])]
        evidence = []
        paths = []
        source_path_count = 0
        truncated = False
        visited = {node["id"]}
        frontier = {node["id"]}
        per_step_limit = min(max(limit_edges, limit_nodes * 2), 500)
        for depth in range(1, hops + 1):
            if not frontier or len(visual_edges) >= limit_edges or len(visual_nodes) >= limit_nodes:
                break
            remaining_edges = limit_edges - len(visual_edges)
            rows = self._neighborhood_step(
                frontier,
                relation_types,
                direction,
                req["project_id"],
                min(per_step_limit, remaining_edges),
            )
            if not rows:
                break
            next_frontier = set()
            for row in rows:
                edge = row.get("r") or {}
                current = row.get("current") or {}
                neighbor = row.get("neighbor") or {}
                current_id = current.get("id")
                neighbor_id = neighbor.get("id")
                if not current_id or not neighbor_id:
                    continue
                if len(visual_edges) >= limit_edges:
                    truncated = True
                    break
                if current_id not in visual_nodes:
                    if len(visual_nodes) >= limit_nodes:
                        truncated = True
                        continue
                    visual_nodes[current_id] = self._visual_node(current, depth - 1, req["include_properties"])
                    entities.append(entity_ref(current, req["include_properties"]))
                if neighbor_id not in visual_nodes:
                    if len(visual_nodes) >= limit_nodes:
                        truncated = True
                        continue
                    visual_nodes[neighbor_id] = self._visual_node(neighbor, depth, req["include_properties"])
                    entities.append(entity_ref(neighbor, req["include_properties"]))
                else:
                    visual_nodes[neighbor_id]["depth"] = min(visual_nodes[neighbor_id]["depth"], depth)
                source = current_id
                target = neighbor_id
                key = (source, target, edge.get("type"), edge.get("id"))
                if key not in visual_edges:
                    visual_edges[key] = self._visual_edge(edge, source, target)
                    source_path_count += 1
                    paths.append(self._edge_path_payload(edge, source, target, direction))
                    evidence.append(self._edge_evidence(edge))
                if neighbor_id not in visited:
                    next_frontier.add(neighbor_id)
            visited.update(next_frontier)
            frontier = next_frontier
            if len(rows) >= min(per_step_limit, remaining_edges):
                truncated = True
        status = "partial" if truncated else "ok"
        warns = []
        if hops < req["max_hops"]:
            warns.append(warning("MAX_HOPS_CAPPED", f"局部图展示深度已从{req['max_hops']}限制为{hops}。"))
            status = "partial"
        if truncated:
            warns.append(warning("RESULT_TRUNCATED", "局部图按层展开时达到节点或关系上限，结果已截断。"))
        if not visual_edges:
            status = "partial"
            warns.append(warning("NEIGHBORHOOD_EMPTY", "目标实体在当前关系范围内没有展开到邻接关系。", related_entity_ids=[node["id"]]))
        visual_graph = {
            "nodes": sorted(visual_nodes.values(), key=lambda item: (item["depth"], item["type"], item["id"])),
            "edges": list(visual_edges.values()),
        }
        return response(
            "get_graph_neighborhood",
            status=status,
            answer=f"围绕“{_display_name(node)}”展开了{len(visual_graph['nodes'])}个节点、{len(visual_graph['edges'])}条关系。",
            data={
                "center_entity_id": node["id"],
                "direction": direction,
                "max_hops": hops,
                "relation_profile": profile,
                "edge_types": relation_types,
                "source_path_count": source_path_count,
                "summary": {
                    "node_count": len(visual_graph["nodes"]),
                    "edge_count": len(visual_graph["edges"]),
                    "truncated": truncated,
                },
                "visual_graph": visual_graph,
            },
            entities=_dedupe_entities(entities),
            paths=paths[: min(req["limit"], 100)],
            evidence=evidence[:limit_edges] if req["include_evidence"] else [],
            warnings=warns,
            page={"limit": limit_nodes, "returned": len(visual_graph["nodes"]), "next_cursor": None, "has_more": truncated},
        )

    def explain_lineage_path(self, req: dict) -> dict:
        from_id = req.get("from_entity_id")
        to_id = req.get("to_entity_id")
        if not from_id or not to_id:
            raise ValueError("from_entity_id and to_entity_id are required")
        hops = req["max_hops"]
        relations = "DEPENDS_ON|PRODUCES|CONSUMES|EMITS_SQL|READS|WRITES|DATASET_DEPENDS_ON|HAS_COLUMN|DERIVED_FROM|INFLUENCED_BY|STORED_IN|COMPUTED_BY|HAS_DEFINITION|HAS_CODE_DEFINITION|HAS_COMPARISON"
        rows = self.store.query(
            f"MATCH (a:KGNode {{id: $from_id}}), (b:KGNode {{id: $to_id}}) MATCH p=shortestPath((a)-[:{relations}*..{hops}]-(b)) RETURN p LIMIT 1",
            {"from_id": from_id, "to_id": to_id},
        )
        if not rows:
            return response("explain_lineage_path", status="not_found", answer="两个实体之间未找到可解释路径。")
        path, entities, evidence = self._path_payload(rows[0]["p"], "connection", req["include_properties"])
        explanations = []
        for edge in rows[0]["p"].get("edges", []):
            explanations.append({"edge_id": edge.get("id"), "type": edge.get("type"), "reason": edge.get("properties", {}).get("source_type") or edge.get("properties", {}).get("fact_type"), "confidence": edge.get("properties", {}).get("confidence", "medium")})
        return response("explain_lineage_path", answer=f"找到一条{path['hop_count']}跳的最短路径。", data={"from_entity_id": from_id, "to_entity_id": to_id, "step_explanations": explanations}, entities=entities, paths=[path], evidence=evidence if req["include_evidence"] else [])
