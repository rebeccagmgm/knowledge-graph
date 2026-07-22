from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .branch_structure import (
    STRUCTURAL_RELATIONSHIP_TYPES,
    attach_structural_summaries,
)
from .contracts import (
    CONFIDENCE_RANK,
    compact_properties,
    encode_cursor,
    request_id,
    response,
    validate_common,
    warning,
)


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
    "column": "DERIVED_FROM",
    "metric": "STORED_IN|COMPUTED_BY|DATASET_DEPENDS_ON|DEPENDS_ON",
}

COMPARE_BRANCH_BATCH_SIZE = 10
COMPARE_MEMORY_ERROR_CODE = "Neo.TransientError.General.MemoryPoolOutOfMemoryError"
COMPARE_QUERY_TIMEOUT_SECONDS = 30.0

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
        "explain_lineage_path", "compare_branches",
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
        if "diagnostics" not in result:
            return result
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

    def get_graph_native_capabilities(self, project_id: str | None = None) -> dict:
        context = self.get_graph_status(project_id)
        return {
            "provider_id": "graph-query-provider",
            "contract_version": "graph-native:v1",
            "supported_primitives": ["compare_branches"],
            "primitive_capabilities": {
                "compare_branches": {
                    "entity_layers": [
                        "schedule_task", "dataset", "metric", "metric_definition",
                        "sql_statement", "result_column",
                    ],
                    "derived_comparisons": [
                        "partial_sharing", "pairwise_similarity",
                        "result_column_derivation_differences",
                        "observed_shared_group_connectivity",
                    ],
                    "unsupported_entity_layers": ["expression"],
                    "execution_recovery": ["adaptive_branch_bisection"],
                },
            },
            "graph_context": {
                "project_id": context["project_id"],
                "build_id": context.get("build_id"),
                "built_at": context.get("built_at"),
                "is_latest": bool(context.get("is_latest")),
                "has_pending_change": bool(context.get("has_pending_change")),
            },
        }

    @staticmethod
    def _validate_compare_branches_request(req: dict) -> list[str]:
        if req.get("contract_version") != "graph-native:v1":
            raise ValueError("contract_version must be graph-native:v1")
        graph_build_id = req.get("graph_build_id")
        if not isinstance(graph_build_id, str) or not graph_build_id:
            raise ValueError("graph_build_id is required")
        branch_ids = req.get("confirmed_branch_ids")
        if not isinstance(branch_ids, list) or len(branch_ids) < 2:
            raise ValueError("confirmed_branch_ids must contain at least two branches")
        if len(branch_ids) > 100 or len(set(branch_ids)) != len(branch_ids):
            raise ValueError("confirmed_branch_ids must contain 2 to 100 unique branches")
        if any(not isinstance(branch_id, str) or not re.fullmatch(r"task:[^\s]+", branch_id) for branch_id in branch_ids):
            raise ValueError("confirmed_branch_ids must contain task entity ids")
        if req["limit"] > 100:
            raise ValueError("limit must be between 1 and 100 for graph-native requests")
        return branch_ids

    @staticmethod
    def _compare_shared_groups(
        branch_ids: list[str],
        entity_sets_by_branch: dict[str, dict[str, set[str]]],
        limit: int,
    ) -> tuple[list[dict], int]:
        memberships: dict[str, dict[str, set[str]]] = {}
        for branch_id, typed_sets in entity_sets_by_branch.items():
            for entity_type, entity_ids in typed_sets.items():
                for entity_id in entity_ids:
                    memberships.setdefault(entity_id, {
                        "entity_type": entity_type,
                        "branch_ids": set(),
                    })["branch_ids"].add(branch_id)

        grouped: dict[tuple[str, ...], dict[str, list[str]]] = {}
        for entity_id, membership in memberships.items():
            member_ids = tuple(
                branch_id for branch_id in branch_ids
                if branch_id in membership["branch_ids"]
            )
            if not 2 <= len(member_ids) < len(branch_ids):
                continue
            grouped.setdefault(member_ids, {}).setdefault(
                str(membership["entity_type"]), []
            ).append(entity_id)

        groups = []
        for member_ids, typed_ids in grouped.items():
            sorted_typed_ids = {
                entity_type: sorted(entity_ids)
                for entity_type, entity_ids in sorted(typed_ids.items())
            }
            groups.append({
                "branch_ids": list(member_ids),
                "shared_branch_count": len(member_ids),
                "entity_count": sum(len(entity_ids) for entity_ids in sorted_typed_ids.values()),
                "entity_counts_by_type": {
                    entity_type: len(entity_ids)
                    for entity_type, entity_ids in sorted_typed_ids.items()
                },
                "entity_ids_by_type": sorted_typed_ids,
                "evidence_status": "CONFIRMED",
            })
        groups.sort(key=lambda item: (
            -item["shared_branch_count"],
            -item["entity_count"],
            item["branch_ids"],
        ))
        return groups[:limit], max(0, len(groups) - limit)

    @staticmethod
    def _compare_pairwise(
        branch_ids: list[str],
        entity_sets_by_branch: dict[str, dict[str, set[str]]],
        limit: int,
    ) -> tuple[list[dict], int]:
        comparisons = []
        for left_index, left_id in enumerate(branch_ids):
            for right_id in branch_ids[left_index + 1:]:
                left_sets = entity_sets_by_branch[left_id]
                right_sets = entity_sets_by_branch[right_id]
                entity_types = sorted(set(left_sets) | set(right_sets))
                shared_by_type = {}
                union_by_type = {}
                left_all: set[str] = set()
                right_all: set[str] = set()
                for entity_type in entity_types:
                    left_values = left_sets.get(entity_type, set())
                    right_values = right_sets.get(entity_type, set())
                    shared_by_type[entity_type] = len(left_values & right_values)
                    union_by_type[entity_type] = len(left_values | right_values)
                    left_all.update(left_values)
                    right_all.update(right_values)
                union = left_all | right_all
                shared = left_all & right_all
                comparisons.append({
                    "branch_ids": [left_id, right_id],
                    "shared_entity_count": len(shared),
                    "union_entity_count": len(union),
                    "jaccard_similarity": round(len(shared) / len(union), 6) if union else None,
                    "shared_counts_by_type": shared_by_type,
                    "union_counts_by_type": union_by_type,
                    "evidence_status": "DERIVED_FROM_CONFIRMED_ENTITIES",
                })
        comparisons.sort(key=lambda item: (
            -(item["jaccard_similarity"] or 0),
            item["branch_ids"],
        ))
        return comparisons[:limit], max(0, len(comparisons) - limit)

    @staticmethod
    def _compare_column_derivations(rows: list[dict]) -> tuple[list[dict], list[dict]]:
        by_column: dict[str, dict[str, set[str]]] = {}
        column_ids_by_branch: dict[str, set[str]] = {}
        for row in rows:
            branch_id = row.get("branch_id")
            column_id = row.get("column_id")
            column_key = row.get("column_key") or column_id
            if not branch_id or not column_id or not column_key:
                continue
            column_ids_by_branch.setdefault(branch_id, set()).add(column_id)
            by_column.setdefault(str(column_key), {}).setdefault(branch_id, set()).update(
                source_id for source_id in row.get("source_column_ids", []) if source_id
            )

        differences = []
        incomplete = []
        for column_key, sources_by_branch in sorted(by_column.items()):
            if len(sources_by_branch) < 2:
                continue
            missing_branches = sorted(
                branch_id for branch_id, source_ids in sources_by_branch.items()
                if not source_ids
            )
            if missing_branches:
                incomplete.append({
                    "column_key": column_key,
                    "branch_ids": sorted(sources_by_branch),
                    "missing_derivation_branch_ids": missing_branches,
                    "evidence_status": "MISSING",
                })
                continue
            normalized = {tuple(sorted(source_ids)) for source_ids in sources_by_branch.values()}
            if len(normalized) > 1:
                differences.append({
                    "column_key": column_key,
                    "evidence_status": "CONFIRMED",
                    "derivations_by_branch": {
                        branch_id: sorted(source_ids)
                        for branch_id, source_ids in sorted(sources_by_branch.items())
                    },
                })
        return differences, incomplete

    def _query_compare_branch_scope_once(
        self,
        branch_ids: list[str],
        project_id: str,
        max_depth: int,
    ) -> list[dict]:
        return self.store.query(
            f"""
            /* compare_branches:scope */
            UNWIND $branch_entity_ids AS branch_id
            MATCH (branch:ScheduleTask {{id: branch_id, project_key: $project_id}})
            OPTIONAL MATCH task_path=(branch)-[:DEPENDS_ON*0..{max_depth}]->(
                task:ScheduleTask {{project_key: $project_id}}
            )
            WITH branch_id, branch, collect(DISTINCT task) AS tasks,
                 collect(DISTINCT task_path) AS task_paths
            UNWIND tasks AS task
            OPTIONAL MATCH (task)-[emits_rel:EMITS_SQL]->(:SqlStatement)-[read_rel:READS]->(
                consumed:Dataset {{project_key: $project_id}}
            )
            OPTIONAL MATCH (task)-[produced_rel:PRODUCES]->(
                produced:Dataset {{project_key: $project_id}}
            )
            WITH branch_id, branch, tasks, task_paths,
                 collect(DISTINCT task.id) AS task_ids,
                 collect(DISTINCT consumed) AS consumed_datasets,
                 collect(DISTINCT produced) AS produced_datasets,
                 collect(DISTINCT emits_rel) AS emits_rels,
                 collect(DISTINCT read_rel) AS read_rels,
                 collect(DISTINCT produced_rel) AS produced_rels
            OPTIONAL MATCH (branch)-[:PRODUCES]->(
                result_dataset:Dataset {{project_key: $project_id}}
            )
            WITH branch_id, branch, task_ids, task_paths, consumed_datasets,
                 produced_datasets, emits_rels, read_rels, produced_rels,
                 collect(DISTINCT result_dataset) AS result_datasets
            WITH branch_id, branch, task_ids, task_paths, consumed_datasets,
                 produced_datasets, emits_rels, read_rels, produced_rels, result_datasets,
                 [dataset IN result_datasets | dataset.id] AS result_dataset_ids,
                 consumed_datasets + CASE
                     WHEN size(result_datasets) > 0 THEN result_datasets
                     ELSE produced_datasets
                 END AS lineage_roots
            UNWIND CASE WHEN size(lineage_roots) = 0 THEN [null] ELSE lineage_roots END AS direct_dataset
            OPTIONAL MATCH dataset_path=(direct_dataset)-[:DATASET_DEPENDS_ON*0..{max_depth}]->(
                lineage_dataset:Dataset {{project_key: $project_id}}
            )
            WITH branch_id, branch, task_ids, task_paths, consumed_datasets,
                 produced_datasets, emits_rels, read_rels, produced_rels, result_dataset_ids,
                 collect(DISTINCT lineage_dataset.id) AS lineage_dataset_ids,
                 collect(DISTINCT dataset_path) AS dataset_paths
            RETURN branch_id, task_ids,
                   [dataset IN consumed_datasets WHERE dataset IS NOT NULL | dataset.id] AS consumed_dataset_ids,
                   [dataset IN produced_datasets WHERE dataset IS NOT NULL | dataset.id] AS produced_dataset_ids,
                   lineage_dataset_ids, result_dataset_ids,
                   reduce(rel_ids = [], path IN task_paths + dataset_paths |
                       rel_ids + [rel IN relationships(path) |
                           coalesce(toString(rel.id), elementId(rel))])
                   + [rel IN emits_rels + read_rels + produced_rels WHERE rel IS NOT NULL |
                       coalesce(toString(rel.id), elementId(rel))] AS relationship_ids,
                   properties(branch) AS branch_properties
            ORDER BY branch_id
            """,
            {"branch_entity_ids": branch_ids, "project_id": project_id},
            timeout_seconds=COMPARE_QUERY_TIMEOUT_SECONDS,
        )

    def _query_compare_branch_scope_adaptive(
        self,
        branch_ids: list[str],
        project_id: str,
        max_depth: int,
        execution: dict,
    ) -> list[dict]:
        execution["attempted_batch_sizes"].append(len(branch_ids))
        try:
            return self._query_compare_branch_scope_once(
                branch_ids,
                project_id,
                max_depth,
            )
        except Exception as exc:  # noqa: BLE001
            error_code = getattr(exc, "code", None)
            if error_code != COMPARE_MEMORY_ERROR_CODE or len(branch_ids) <= 1:
                raise
            execution["memory_recovery_count"] += 1
            if error_code not in execution["recovered_error_codes"]:
                execution["recovered_error_codes"].append(error_code)
            midpoint = len(branch_ids) // 2
            left_rows = self._query_compare_branch_scope_adaptive(
                branch_ids[:midpoint],
                project_id,
                max_depth,
                execution,
            )
            right_rows = self._query_compare_branch_scope_adaptive(
                branch_ids[midpoint:],
                project_id,
                max_depth,
                execution,
            )
            return left_rows + right_rows

    def _query_compare_branch_scopes(
        self,
        branch_ids: list[str],
        project_id: str,
        max_depth: int,
    ) -> tuple[list[dict], dict]:
        execution = {
            "strategy": "single_query",
            "requested_branch_count": len(branch_ids),
            "requested_max_depth": max_depth,
            "applied_max_depth": max_depth,
            "attempted_batch_sizes": [],
            "attempt_count": 0,
            "memory_recovery_count": 0,
            "recovered_error_codes": [],
            "semantic_scope_preserved": True,
        }
        rows = []
        initial_batches = [
            branch_ids[index:index + COMPARE_BRANCH_BATCH_SIZE]
            for index in range(0, len(branch_ids), COMPARE_BRANCH_BATCH_SIZE)
        ]
        for batch in initial_batches:
            rows.extend(self._query_compare_branch_scope_adaptive(
                batch,
                project_id,
                max_depth,
                execution,
            ))
        execution["attempt_count"] = len(execution["attempted_batch_sizes"])
        if execution["memory_recovery_count"]:
            execution["strategy"] = "adaptive_bisection"
        elif len(initial_batches) > 1:
            execution["strategy"] = "bounded_batches"
        return rows, execution

    def compare_branches(self, req: dict) -> dict:
        branch_ids = self._validate_compare_branches_request(req)
        project_id = req.get("project_id", self.project_id)
        max_depth = int(req.get("max_depth", req["max_hops"]))
        if not 1 <= max_depth <= 20:
            raise ValueError("max_depth must be between 1 and 20")
        graph_context = self.get_graph_status(project_id)
        if graph_context.get("build_id") != req["graph_build_id"]:
            raise ValueError("graph_build_id does not match the current graph build")

        rows, execution = self._query_compare_branch_scopes(
            branch_ids,
            project_id,
            max_depth,
        )

        row_by_branch = {row["branch_id"]: row for row in rows}
        legacy_entity_sets = []
        task_sets_by_branch: dict[str, set[str]] = {}
        dataset_sets_by_branch: dict[str, set[str]] = {}
        result_dataset_ids_by_branch: dict[str, set[str]] = {}
        all_task_ids: set[str] = set()
        all_dataset_ids: set[str] = set()
        all_relationship_ids: set[str] = set()
        write_modes = {}
        unresolved = []
        valid_write_modes = {"APPEND", "OVERWRITE", "MERGE"}
        for branch_id in branch_ids:
            row = row_by_branch.get(branch_id)
            if row is None:
                unresolved.append(f"unresolved:{branch_id}")
                legacy_entity_sets.append(set())
                task_sets_by_branch[branch_id] = set()
                dataset_sets_by_branch[branch_id] = set()
                result_dataset_ids_by_branch[branch_id] = set()
                write_modes[branch_id] = "UNKNOWN"
                continue
            task_ids = {value for value in row.get("task_ids", []) if value}
            consumed_ids = {value for value in row.get("consumed_dataset_ids", []) if value}
            produced_ids = {value for value in row.get("produced_dataset_ids", []) if value}
            lineage_ids = {value for value in row.get("lineage_dataset_ids", []) if value}
            dataset_ids = consumed_ids | produced_ids | lineage_ids
            legacy_entity_sets.append(task_ids | dataset_ids)
            task_sets_by_branch[branch_id] = task_ids
            dataset_sets_by_branch[branch_id] = dataset_ids
            result_dataset_ids_by_branch[branch_id] = {
                value for value in row.get("result_dataset_ids", []) if value
            }
            all_task_ids.update(task_ids)
            all_dataset_ids.update(dataset_ids)
            all_relationship_ids.update(
                value for value in row.get("relationship_ids", []) if value
            )
            write_mode = str(
                row.get("write_mode")
                or row.get("branch_properties", {}).get("write_mode")
                or "UNKNOWN"
            ).upper()
            write_modes[branch_id] = write_mode if write_mode in valid_write_modes else "UNKNOWN"

        metric_rows = self.store.query(
            """
            /* compare_branches:metrics */
            MATCH (metric:Metric {project_key: $project_id})
            OPTIONAL MATCH (metric)-[:STORED_IN]->(storage:Dataset)
            OPTIONAL MATCH (metric)-[:COMPUTED_BY]->(compute_task:ScheduleTask)
            OPTIONAL MATCH (metric)-[:HAS_DEFINITION]->(definition:MetricDefinition)
            WITH metric, collect(DISTINCT storage.id) AS storage_dataset_ids,
                 collect(DISTINCT compute_task.id) AS compute_task_ids,
                 collect(DISTINCT definition.id) AS definition_ids
            WHERE any(dataset_id IN storage_dataset_ids WHERE dataset_id IN $dataset_ids)
               OR any(task_id IN compute_task_ids WHERE task_id IN $task_ids)
            RETURN metric.id AS metric_id, storage_dataset_ids, compute_task_ids,
                   definition_ids
            ORDER BY metric_id
            """,
            {
                "project_id": project_id,
                "dataset_ids": sorted(all_dataset_ids),
                "task_ids": sorted(all_task_ids),
            },
            timeout_seconds=COMPARE_QUERY_TIMEOUT_SECONDS,
        )
        sql_rows = self.store.query(
            """
            /* compare_branches:sql */
            MATCH (task:ScheduleTask {project_key: $project_id})-[:EMITS_SQL]->(
                sql:SqlStatement {project_key: $project_id}
            )
            WHERE task.id IN $task_ids
            RETURN task.id AS task_id, collect(DISTINCT sql.id) AS sql_ids
            ORDER BY task_id
            """,
            {"project_id": project_id, "task_ids": sorted(all_task_ids)},
            timeout_seconds=COMPARE_QUERY_TIMEOUT_SECONDS,
        )
        column_rows = self.store.query(
            """
            /* compare_branches:columns */
            MATCH (dataset:Dataset {project_key: $project_id})-[:HAS_COLUMN]->(
                column:Column {project_key: $project_id}
            )
            WHERE dataset.id IN $dataset_ids
            RETURN dataset.id AS dataset_id, collect(DISTINCT column.id) AS column_ids
            ORDER BY dataset_id
            """,
            {"project_id": project_id, "dataset_ids": sorted(all_dataset_ids)},
            timeout_seconds=COMPARE_QUERY_TIMEOUT_SECONDS,
        )
        result_column_rows = self.store.query(
            """
            /* compare_branches:result_columns */
            UNWIND $branch_results AS branch_result
            UNWIND branch_result.result_dataset_ids AS result_dataset_id
            MATCH (result_dataset:Dataset {
                id: result_dataset_id, project_key: $project_id
            })-[:HAS_COLUMN]->(column:Column {project_key: $project_id})
            OPTIONAL MATCH (column)-[:DERIVED_FROM]->(
                source:Column {project_key: $project_id}
            )
            RETURN branch_result.branch_id AS branch_id,
                   result_dataset_id AS result_dataset_id,
                   column.id AS column_id,
                   coalesce(column.name, column.id) AS column_key,
                   collect(DISTINCT source.id) AS source_column_ids
            ORDER BY branch_id, result_dataset_id, column_key
            """,
            {
                "project_id": project_id,
                "branch_results": [
                    {
                        "branch_id": branch_id,
                        "result_dataset_ids": sorted(result_dataset_ids_by_branch[branch_id]),
                    }
                    for branch_id in branch_ids
                ],
            },
            timeout_seconds=COMPARE_QUERY_TIMEOUT_SECONDS,
        )

        metrics_by_branch = {branch_id: set() for branch_id in branch_ids}
        definitions_by_branch = {branch_id: set() for branch_id in branch_ids}
        for row in metric_rows:
            metric_id = row.get("metric_id")
            if not metric_id:
                continue
            storage_ids = {value for value in row.get("storage_dataset_ids", []) if value}
            compute_ids = {value for value in row.get("compute_task_ids", []) if value}
            definition_ids = {value for value in row.get("definition_ids", []) if value}
            for branch_id in branch_ids:
                if (
                    storage_ids & dataset_sets_by_branch[branch_id]
                    or compute_ids & task_sets_by_branch[branch_id]
                ):
                    metrics_by_branch[branch_id].add(metric_id)
                    definitions_by_branch[branch_id].update(definition_ids)

        sql_by_task = {
            row.get("task_id"): {value for value in row.get("sql_ids", []) if value}
            for row in sql_rows if row.get("task_id")
        }
        sql_by_branch = {
            branch_id: set().union(*(
                sql_by_task.get(task_id, set())
                for task_id in task_sets_by_branch[branch_id]
            )) if task_sets_by_branch[branch_id] else set()
            for branch_id in branch_ids
        }
        columns_by_dataset = {
            row.get("dataset_id"): {value for value in row.get("column_ids", []) if value}
            for row in column_rows if row.get("dataset_id")
        }
        scoped_columns_by_branch = {
            branch_id: set().union(*(
                columns_by_dataset.get(dataset_id, set())
                for dataset_id in dataset_sets_by_branch[branch_id]
            )) if dataset_sets_by_branch[branch_id] else set()
            for branch_id in branch_ids
        }
        result_columns_by_branch = {branch_id: set() for branch_id in branch_ids}
        for row in result_column_rows:
            if row.get("branch_id") in result_columns_by_branch and row.get("column_id"):
                result_columns_by_branch[row["branch_id"]].add(row["column_id"])

        entity_sets_by_branch = {
            branch_id: {
                "schedule_task": task_sets_by_branch[branch_id],
                "dataset": dataset_sets_by_branch[branch_id],
                "metric": metrics_by_branch[branch_id],
                "metric_definition": definitions_by_branch[branch_id],
                "sql_statement": sql_by_branch[branch_id],
                "result_column": result_columns_by_branch[branch_id],
            }
            for branch_id in branch_ids
        }
        common_ids = set.intersection(*legacy_entity_sets) if legacy_entity_sets else set()
        divergence_ids = (all_task_ids | all_dataset_ids) - common_ids
        unique_by_branch = {}
        for index, branch_id in enumerate(branch_ids):
            other_ids = set().union(
                *(entity_set for other_index, entity_set in enumerate(legacy_entity_sets)
                  if other_index != index)
            )
            unique_by_branch[branch_id] = sorted(legacy_entity_sets[index] - other_ids)

        unique_by_branch_by_type = {}
        common_by_type = {}
        divergence_by_type = {}
        comparison_entity_types = sorted(next(iter(entity_sets_by_branch.values())))
        for entity_type in comparison_entity_types:
            typed_sets = [
                entity_sets_by_branch[branch_id][entity_type]
                for branch_id in branch_ids
            ]
            typed_common = set.intersection(*typed_sets) if typed_sets else set()
            common_by_type[entity_type] = sorted(typed_common)
            divergence_by_type[entity_type] = sorted(set().union(*typed_sets) - typed_common)
        for branch_id in branch_ids:
            unique_by_branch_by_type[branch_id] = {}
            for entity_type, entity_ids in entity_sets_by_branch[branch_id].items():
                other_ids = set().union(*(
                    entity_sets_by_branch[other_id].get(entity_type, set())
                    for other_id in branch_ids if other_id != branch_id
                ))
                unique_by_branch_by_type[branch_id][entity_type] = sorted(entity_ids - other_ids)

        limit = req["limit"]
        shared_groups, omitted_shared_group_count = self._compare_shared_groups(
            branch_ids, entity_sets_by_branch, limit
        )
        pairwise_comparisons, omitted_pairwise_count = self._compare_pairwise(
            branch_ids, entity_sets_by_branch, limit
        )
        column_differences, incomplete_column_derivations = self._compare_column_derivations(
            result_column_rows
        )
        truncated = bool(omitted_shared_group_count or omitted_pairwise_count)
        warnings = []
        if unresolved:
            warnings.append(
                warning(
                    "LINEAGE_PARTIAL",
                    "One or more confirmed branches could not be resolved in the selected graph build.",
                    related_entity_ids=[item.removeprefix("unresolved:") for item in unresolved],
                )
            )
        if truncated:
            warnings.append(warning(
                "RESULT_TRUNCATED",
                "Some shared-group or pairwise detail rows were omitted; aggregate counts still cover the full branch scope.",
            ))
        if execution["memory_recovery_count"]:
            warnings.append(warning(
                "QUERY_RECOVERED_BY_BATCH_SPLIT",
                "The requested branch scope was preserved after recovering from transaction memory pressure by splitting physical query batches.",
                severity="info",
                related_entity_ids=branch_ids,
            ))

        topology_scope = {
            "max_depth": max_depth,
            "relationship_types": list(STRUCTURAL_RELATIONSHIP_TYPES),
            "semantics": (
                "LITERAL_RELATIONSHIP_INSTANCES_IN_EXACT_MEMBERSHIP_"
                "INDUCED_SUBGRAPH"
            ),
            "evidence_status": "UNAVAILABLE",
        }
        topology_unavailable = bool(unresolved)
        if unresolved:
            warnings.append(warning(
                "STRUCTURAL_TOPOLOGY_UNAVAILABLE",
                "Structural summaries require every requested branch to resolve; core comparison fields remain available.",
                related_entity_ids=branch_ids,
            ))
        else:
            structural_entity_ids_by_branch = {
                branch_id: set() for branch_id in branch_ids
            }
            for group in shared_groups:
                group_entity_ids = {
                    entity_id
                    for entity_ids in group["entity_ids_by_type"].values()
                    for entity_id in entity_ids
                }
                for branch_id in group["branch_ids"]:
                    structural_entity_ids_by_branch[branch_id].update(
                        group_entity_ids
                    )
            relationship_types = "|".join(STRUCTURAL_RELATIONSHIP_TYPES)
            try:
                topology_rows = self.store.query(
                    f"""
                    /* compare_branches:structural_topology */
                    UNWIND $branch_scopes AS branch_scope
                    MATCH (source:KGNode {{project_key: $project_id}})-[
                        relationship:{relationship_types}
                    ]->(target:KGNode {{project_key: $project_id}})
                    WHERE source.id IN branch_scope.entity_ids
                      AND target.id IN branch_scope.entity_ids
                    WITH relationship, source, target,
                         collect(DISTINCT branch_scope.branch_id)
                             AS observed_branch_ids
                    RETURN coalesce(
                               toString(relationship.id),
                               elementId(relationship)
                           ) AS relationship_id,
                           type(relationship) AS relationship_type,
                           source.id AS from_entity_id,
                           target.id AS to_entity_id,
                           observed_branch_ids
                    ORDER BY relationship_id, relationship_type,
                             from_entity_id, to_entity_id
                    """,
                    {
                        "project_id": project_id,
                        "branch_scopes": [
                            {
                                "branch_id": branch_id,
                                "entity_ids": sorted(
                                    structural_entity_ids_by_branch[branch_id]
                                ),
                            }
                            for branch_id in branch_ids
                        ],
                    },
                    timeout_seconds=COMPARE_QUERY_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001
                topology_unavailable = True
                warnings.append(warning(
                    "STRUCTURAL_TOPOLOGY_UNAVAILABLE",
                    "The structural topology query did not complete; core comparison fields remain available.",
                    related_entity_ids=branch_ids,
                ))
            else:
                shared_groups = attach_structural_summaries(
                    shared_groups, topology_rows
                )
                topology_scope["evidence_status"] = "OBSERVED"

        status = (
            "partial"
            if truncated or unresolved or topology_unavailable
            else "ok"
        )
        all_metric_ids = set().union(*metrics_by_branch.values()) if metrics_by_branch else set()
        all_definition_ids = set().union(*definitions_by_branch.values()) if definitions_by_branch else set()
        all_sql_ids = set().union(*sql_by_branch.values()) if sql_by_branch else set()
        all_column_ids = set().union(*scoped_columns_by_branch.values()) if scoped_columns_by_branch else set()
        counts = {
            "tasks": len(all_task_ids),
            "tables": len(all_dataset_ids),
            "columns": len(all_column_ids),
            "expressions": None,
            "datasets": len(all_dataset_ids),
            "metrics": len(all_metric_ids),
            "metric_definitions": len(all_definition_ids),
            "sql_statements": len(all_sql_ids),
            "result_columns": len(set().union(*result_columns_by_branch.values())),
            "result_branches": len(row_by_branch),
        }
        stable_context = {
            "project_id": project_id,
            "build_id": graph_context["build_id"],
            "built_at": graph_context.get("built_at"),
            "is_latest": bool(graph_context.get("is_latest")),
            "has_pending_change": bool(graph_context.get("has_pending_change")),
        }
        return {
            "request_id": request_id(),
            "primitive": "compare_branches",
            "status": status,
            "data": {
                "aggregation_stage": "before_pagination",
                "scope_branch_ids": branch_ids,
                "execution": execution,
                "component_counts": counts,
                "coverage": {
                    "nodes_considered": sum(
                        value for key, value in counts.items()
                        if key not in {"tables", "expressions", "result_branches"}
                        and key != "result_columns"
                        and isinstance(value, int)
                    ),
                    "relationships_considered": len(all_relationship_ids),
                    "permission_filtered": False,
                    "unresolved_segments": len(unresolved),
                },
                "truncated": truncated,
                "results": [{
                    "branch_ids": branch_ids,
                    "common_entity_ids": sorted(common_ids),
                    "divergence_entity_ids": sorted(divergence_ids),
                    "unique_entity_ids_by_branch": unique_by_branch,
                    "entity_sets_by_branch": {
                        branch_id: {
                            entity_type: sorted(entity_ids)
                            for entity_type, entity_ids in typed_sets.items()
                        }
                        for branch_id, typed_sets in entity_sets_by_branch.items()
                    },
                    "common_entity_ids_by_type": common_by_type,
                    "divergence_entity_ids_by_type": divergence_by_type,
                    "unique_entity_ids_by_branch_by_type": unique_by_branch_by_type,
                    "partially_shared_entity_groups": shared_groups,
                    "omitted_shared_group_count": omitted_shared_group_count,
                    "topology_scope": topology_scope,
                    "pairwise_comparisons": pairwise_comparisons,
                    "omitted_pairwise_comparison_count": omitted_pairwise_count,
                    "write_mode_by_branch": write_modes,
                    "column_derivation_differences": column_differences,
                    "incomplete_column_derivations": incomplete_column_derivations,
                    "evidence_status": {
                        "entity_layers": {
                            "schedule_task": "CONFIRMED",
                            "dataset": "CONFIRMED",
                            "metric": "CONFIRMED",
                            "metric_definition": "CONFIRMED",
                            "sql_statement": "CONFIRMED",
                            "result_column": "CONFIRMED",
                        },
                        "tables": "DATASET_NODE_PROXY",
                        "expressions": "UNSUPPORTED",
                        "write_mode_by_branch": {
                            branch_id: "CONFIRMED" if write_mode != "UNKNOWN" else "MISSING"
                            for branch_id, write_mode in write_modes.items()
                        },
                        "pairwise_comparisons": "DERIVED_FROM_CONFIRMED_ENTITIES",
                    },
                    "evidence_gaps": [
                        {
                            "code": "WRITE_MODE_MISSING",
                            "branch_id": branch_id,
                            "evidence_status": "MISSING",
                            "meaning": "No supported write-mode property was found; UNKNOWN is not a business conclusion.",
                        }
                        for branch_id, write_mode in write_modes.items()
                        if write_mode == "UNKNOWN"
                    ] + [
                        {
                            "code": "REGISTERED_METRIC_LINK_NOT_FOUND",
                            "branch_id": branch_id,
                            "evidence_status": "MISSING",
                            "meaning": "No linked Metric node was found in this branch scope; this does not prove the report has no business indicators.",
                        }
                        for branch_id in branch_ids
                        if not metrics_by_branch[branch_id]
                    ] + [
                        {
                            "code": "SQL_STATEMENT_LINK_NOT_FOUND",
                            "branch_id": branch_id,
                            "evidence_status": "MISSING",
                            "meaning": "No linked SqlStatement node was found in this branch scope.",
                        }
                        for branch_id in branch_ids
                        if not sql_by_branch[branch_id]
                    ] + [
                        {
                            "code": "RESULT_COLUMN_LINK_NOT_FOUND",
                            "branch_id": branch_id,
                            "evidence_status": "MISSING",
                            "meaning": "The branch has a result dataset but no linked result-column nodes were found.",
                        }
                        for branch_id in branch_ids
                        if result_dataset_ids_by_branch[branch_id]
                        and not result_columns_by_branch[branch_id]
                    ] + ([{
                        "code": "EXPRESSION_ENTITY_UNSUPPORTED",
                        "evidence_status": "UNSUPPORTED",
                        "meaning": "The current graph/query contract has no Expression entity layer.",
                    }] if counts["expressions"] is None else []),
                    "unresolved_segment_ids": unresolved,
                }],
            },
            "warnings": warnings,
            "graph_context": stable_context,
            "page": {
                "limit": limit,
                "returned": 1,
                "next_cursor": None,
                "has_more": truncated,
            },
        }


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
        fulltext_query = " ".join(token for token in FULLTEXT_SAFE.split(query.strip()) if token)
        if fulltext_query:
            try:
                rows = self.store.query(
                    """
                    CALL db.index.fulltext.queryNodes('kg_entity_search', $fulltext_query) YIELD node, score
                    WHERE node:KGNode
                      AND node.project_key = $project_id
                      AND any(label IN labels(node) WHERE label IN $labels)
                    RETURN node AS n, score
                    ORDER BY score DESC, node.id
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
            RETURN n, score
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
            item["match"] = {"score": row["score"], "method": "exact" if row["score"] >= 0.95 else "fuzzy"}
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
        return None, [entity_ref(row["n"], req["include_properties"]) for row in rows]

    def resolve_entity(self, req: dict) -> dict:
        node, candidates = self._resolve_subject(req)
        if node:
            entity = entity_ref(node, req["include_properties"])
            return response("resolve_entity", answer=f"已解析为{entity['display_name']}。", data={"resolved_entity_id": entity["entity_id"]}, entities=[entity])
        if candidates:
            return response("resolve_entity", status="ambiguous", answer="输入对应多个候选实体。", entities=candidates, warnings=[warning("ENTITY_AMBIGUOUS", "请补充实体类型或所属表等上下文。")])
        return response("resolve_entity", status="not_found", answer="未找到目标实体。")

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
        if entity_type == "column":
            rows = self.store.query(
                """
                MATCH (subject:Column {id: $id, project_key: $project_id})
                OPTIONAL MATCH (subject_dataset:Dataset {project_key: $project_id})-[:HAS_COLUMN]->(subject)
                OPTIONAL MATCH path=(subject)<-[:DERIVED_FROM*1..50]-(down_col:Column {project_key: $project_id})
                WHERE length(path) <= $hops
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
                OPTIONAL MATCH (subject)<-[rels:DERIVED_FROM*1..50]-(:Column {project_key: $project_id})
                WHERE size(rels) <= $hops
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
                """,
                params,
            )
        elif entity_type == "dataset":
            rows = self.store.query(
                """
                MATCH (subject:Dataset {id: $id, project_key: $project_id})
                OPTIONAL MATCH path=(subject)<-[:DATASET_DEPENDS_ON*1..50]-(down_dataset:Dataset {project_key: $project_id})
                WHERE length(path) <= $hops
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
                """,
                params,
            )
        elif entity_type == "schedule_task":
            rows = self.store.query(
                """
                MATCH (subject:ScheduleTask {id: $id, project_key: $project_id})
                OPTIONAL MATCH path=(subject)<-[:DEPENDS_ON*1..50]-(down_task:ScheduleTask {project_key: $project_id})
                WHERE length(path) <= $hops
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
                """,
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

    def analyze_impact(self, req: dict) -> dict:
        change_type = req.get("change_type", "logic_change")
        if change_type not in CHANGE_TYPES:
            raise ValueError(f"change_type must be one of {sorted(CHANGE_TYPES)}")
        node, candidates = self._resolve_subject(req, {"schedule_task", "dataset", "column"})
        if not node:
            return self._ambiguity_or_not_found("analyze_impact", candidates)
        aggregate, aggregate_entities = self._impact_aggregate(node, req)
        paths, path_entities, evidence = self._impact_sample_paths(node, req)
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

    def explain_lineage_path(self, req: dict) -> dict:
        from_id = req.get("from_entity_id")
        to_id = req.get("to_entity_id")
        if not from_id or not to_id:
            raise ValueError("from_entity_id and to_entity_id are required")
        hops = req["max_hops"]
        relations = "DEPENDS_ON|PRODUCES|CONSUMES|EMITS_SQL|READS|WRITES|DATASET_DEPENDS_ON|HAS_COLUMN|DERIVED_FROM|STORED_IN|COMPUTED_BY|HAS_DEFINITION|HAS_CODE_DEFINITION|HAS_COMPARISON"
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
