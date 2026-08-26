#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path

from query_layer.contracts import response, validate_common
from query_layer.service import QueryService, entity_ref


class EmptyStore:
    def query(self, cypher, parameters=None):
        return []


class CompareBranchesStore:
    def __init__(self):
        self.calls = []

    def query(self, cypher, parameters=None, timeout_seconds=None):
        parameters = parameters or {}
        self.calls.append((cypher, parameters, timeout_seconds))
        if "compare_branches:scope" in cypher:
            return [
                {
                    "branch_id": "task:one",
                    "task_ids": ["task:one", "task:shared"],
                    "consumed_dataset_ids": ["dataset:common", "dataset:one"],
                    "produced_dataset_ids": ["dataset:result-one"],
                    "lineage_dataset_ids": [
                        "dataset:common", "dataset:one", "dataset:result-one",
                    ],
                    "result_dataset_ids": ["dataset:result-one"],
                    "relationship_ids": ["edge:one", "edge:shared-one"],
                    "write_mode": "OVERWRITE",
                },
                {
                    "branch_id": "task:two",
                    "task_ids": ["task:two", "task:shared"],
                    "consumed_dataset_ids": ["dataset:common", "dataset:two"],
                    "produced_dataset_ids": ["dataset:result-two"],
                    "lineage_dataset_ids": [
                        "dataset:common", "dataset:two", "dataset:result-two",
                    ],
                    "result_dataset_ids": ["dataset:result-two"],
                    "relationship_ids": ["edge:two", "edge:shared-two"],
                    "write_mode": None,
                },
            ]
        if "compare_branches:metrics" in cypher:
            return [
                {
                    "metric_id": "metric:common",
                    "storage_dataset_ids": ["dataset:common"],
                    "compute_task_ids": ["task:shared"],
                    "definition_ids": ["metric-definition:common"],
                },
                {
                    "metric_id": "metric:one",
                    "storage_dataset_ids": ["dataset:one"],
                    "compute_task_ids": ["task:one"],
                    "definition_ids": ["metric-definition:one"],
                },
            ]
        if "compare_branches:sql" in cypher:
            return [
                {"task_id": "task:one", "sql_ids": ["sql:one"]},
                {"task_id": "task:two", "sql_ids": ["sql:two"]},
                {"task_id": "task:shared", "sql_ids": ["sql:shared"]},
            ]
        if "compare_branches:columns" in cypher:
            return [
                {"dataset_id": "dataset:common", "column_ids": ["column:common"]},
                {"dataset_id": "dataset:result-one", "column_ids": ["column:result-one:amount"]},
                {"dataset_id": "dataset:result-two", "column_ids": ["column:result-two:amount"]},
            ]
        if "compare_branches:result_columns" in cypher:
            return [
                {
                    "branch_id": "task:one",
                    "result_dataset_id": "dataset:result-one",
                    "column_id": "column:result-one:amount",
                    "column_key": "amount",
                    "source_column_ids": ["column:source-one"],
                },
                {
                    "branch_id": "task:two",
                    "result_dataset_id": "dataset:result-two",
                    "column_id": "column:result-two:amount",
                    "column_key": "amount",
                    "source_column_ids": ["column:source-two"],
                },
            ]
        if "branch_entity_ids" not in cypher:
            project = {
                "properties": {
                    "build_id": "build-1",
                    "built_at": "2026-07-21T00:00:00Z",
                },
            }
            return [{"p": project, "latest": project}]
        return []


class FakeNeo4jError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class AdaptiveCompareBranchesStore:
    def __init__(self, fail_above=None, error_code=None):
        self.build_id = "build-1"
        self.fail_above = fail_above
        self.error_code = error_code
        self.calls = []
        self.scope_batch_sizes = []
        self.scope_queries = []

    def query(self, cypher, parameters=None, timeout_seconds=None):
        parameters = parameters or {}
        self.calls.append((cypher, parameters, timeout_seconds))
        if "compare_branches:scope" in cypher:
            branch_ids = parameters["branch_entity_ids"]
            self.scope_batch_sizes.append(len(branch_ids))
            self.scope_queries.append(cypher)
            if self.fail_above is not None and len(branch_ids) > self.fail_above:
                raise FakeNeo4jError(self.error_code)
            return [
                {
                    "branch_id": branch_id,
                    "task_ids": [branch_id],
                    "consumed_dataset_ids": [],
                    "produced_dataset_ids": [f"dataset:{branch_id}:result"],
                    "lineage_dataset_ids": [f"dataset:{branch_id}:result"],
                    "result_dataset_ids": [f"dataset:{branch_id}:result"],
                    "relationship_ids": [f"edge:{branch_id}:result"],
                    "branch_properties": {},
                }
                for branch_id in branch_ids
            ]
        if "compare_branches:" in cypher:
            return []
        project = {
            "properties": {
                "build_id": self.build_id,
                "built_at": "2026-07-21T00:00:00Z",
            },
        }
        return [{"p": project, "latest": project}]


class StructuralCompareBranchesStore(AdaptiveCompareBranchesStore):
    def __init__(self, topology_error=None, unresolved_branch_id=None):
        super().__init__()
        self.topology_error = topology_error
        self.unresolved_branch_id = unresolved_branch_id

    def query(self, cypher, parameters=None, timeout_seconds=None):
        parameters = parameters or {}
        if "compare_branches:structural_topology" in cypher:
            self.calls.append((cypher, parameters, timeout_seconds))
            if self.topology_error:
                raise self.topology_error
            return [
                {
                    "relationship_id": "edge:shared",
                    "relationship_type": "DEPENDS_ON",
                    "from_entity_id": "dataset:shared-one",
                    "to_entity_id": "dataset:shared-two",
                    "observed_branch_ids": ["task:0", "task:1"],
                }
            ]
        if "compare_branches:scope" in cypher:
            rows = super().query(cypher, parameters, timeout_seconds)
            result = []
            for row in rows:
                if row["branch_id"] == self.unresolved_branch_id:
                    continue
                if row["branch_id"] in {"task:0", "task:1"}:
                    row = {
                        **row,
                        "consumed_dataset_ids": [
                            "dataset:shared-one", "dataset:shared-two",
                        ],
                        "lineage_dataset_ids": [
                            row["lineage_dataset_ids"][0],
                            "dataset:shared-one",
                            "dataset:shared-two",
                        ],
                    }
                result.append(row)
            return result
        return super().query(cypher, parameters, timeout_seconds)

class RecordingStore:
    def __init__(self):
        self.queries = []

    def query(self, cypher, parameters=None):
        self.queries.append((cypher, parameters or {}))
        return []


class NeighborhoodStore:
    def __init__(self):
        self.queries = []

    def query(self, cypher, parameters=None):
        self.queries.append((cypher, parameters or {}))
        if "MATCH (n:KGNode" in cypher:
            return [
                {
                    "n": {
                        "id": "column:a.x",
                        "labels": ["Column"],
                        "properties": {"name": "x", "project_key": "demo"},
                    }
                }
            ]
        if "MATCH (neighbor:KGNode" in cypher:
            return [
                {
                    "current": {
                        "id": "column:a.x",
                        "labels": ["Column"],
                        "properties": {"name": "x", "project_key": "demo"},
                    },
                    "neighbor": {
                        "id": "column:b.x",
                        "labels": ["Column"],
                        "properties": {"name": "x"},
                    },
                    "r": {
                        "id": "e1",
                        "type": "INFLUENCED_BY",
                        "from": "column:b.x",
                        "to": "column:a.x",
                        "properties": {"confidence": "high", "task_id": "9"},
                    },
                }
            ]
        return []


class QueryLayerTest(unittest.TestCase):
    def test_common_defaults(self):
        result = validate_common({})
        self.assertEqual(result["mode"], "balanced")
        self.assertEqual(result["max_hops"], 20)
        self.assertEqual(result["limit"], 100)

    def test_common_rejects_unbounded_hops(self):
        with self.assertRaises(ValueError):
            validate_common({"max_hops": 51})

    def test_response_has_stable_envelope(self):
        result = response("search_entities", answer="ok")
        self.assertEqual(
            list(result),
            ["request_id", "primitive", "status", "answer", "data", "entities", "paths", "evidence", "warnings", "graph_context", "page", "diagnostics"],
        )

    def test_entity_reference(self):
        node = {"id": "task:1", "labels": ["ScheduleTask"], "properties": {"task_id": "1", "task_name": "测试任务"}}
        result = entity_ref(node)
        self.assertEqual(result["entity_type"], "schedule_task")
        self.assertEqual(result["display_name"], "测试任务")

    def test_path_payload_preserves_evidence_fields(self):
        service = QueryService(EmptyStore())
        path = {
            "nodes": [
                {"id": "column:a.x", "labels": ["Column"], "properties": {"name": "x"}},
                {"id": "column:b.x", "labels": ["Column"], "properties": {"name": "x"}},
            ],
            "edges": [
                {"id": "e1", "type": "DERIVED_FROM", "from": "column:b.x", "to": "column:a.x", "properties": {"confidence": "medium", "task_id": "9", "statement_id": "sql:9"}},
            ],
        }
        result, _, evidence = service._path_payload(path, "downstream", True)
        self.assertEqual(result["edges"][0]["task_id"], "9")
        self.assertEqual(evidence[0]["task_id"], "9")

    def test_unknown_primitive_returns_protocol_error(self):
        service = QueryService(EmptyStore())
        result = service.execute("drop_database", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["warnings"][0]["code"], "INVALID_REQUEST")

    def test_compare_branches_aggregates_full_scope_before_limiting_ids(self):
        store = CompareBranchesStore()
        service = QueryService(store, project_id="project-one")

        result = service.execute(
            "compare_branches",
            {
                "project_id": "project-one",
                "contract_version": "graph-native:v1",
                "graph_build_id": "build-1",
                "confirmed_branch_ids": ["task:one", "task:two"],
                "limit": 2,
                "max_depth": 2,
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["aggregation_stage"], "before_pagination")
        self.assertEqual(result["data"]["scope_branch_ids"], ["task:one", "task:two"])
        self.assertEqual(
            result["data"]["results"][0]["common_entity_ids"],
            ["dataset:common", "task:shared"],
        )
        self.assertEqual(
            result["data"]["results"][0]["divergence_entity_ids"],
            [
                "dataset:one",
                "dataset:result-one",
                "dataset:result-two",
                "dataset:two",
                "task:one",
                "task:two",
            ],
        )
        self.assertEqual(
            result["data"]["results"][0]["unique_entity_ids_by_branch"],
            {
                "task:one": ["dataset:one", "dataset:result-one", "task:one"],
                "task:two": ["dataset:result-two", "dataset:two", "task:two"],
            },
        )
        self.assertEqual(
            result["data"]["results"][0]["write_mode_by_branch"],
            {"task:one": "OVERWRITE", "task:two": "UNKNOWN"},
        )
        self.assertEqual(result["data"]["component_counts"]["tasks"], 3)
        self.assertEqual(result["data"]["component_counts"]["datasets"], 5)
        self.assertEqual(result["data"]["component_counts"]["tables"], 5)
        self.assertEqual(result["data"]["component_counts"]["columns"], 3)
        self.assertEqual(result["data"]["component_counts"]["metrics"], 2)
        self.assertEqual(result["data"]["component_counts"]["metric_definitions"], 2)
        self.assertEqual(result["data"]["component_counts"]["sql_statements"], 3)
        self.assertIsNone(result["data"]["component_counts"]["expressions"])
        comparison = result["data"]["results"][0]
        self.assertNotIn("comparison_schema_version", comparison)
        self.assertEqual(
            comparison["entity_sets_by_branch"]["task:one"]["metric"],
            ["metric:common", "metric:one"],
        )
        self.assertEqual(
            comparison["entity_sets_by_branch"]["task:two"]["metric"],
            ["metric:common"],
        )
        self.assertEqual(comparison["pairwise_comparisons"][0]["shared_entity_count"], 5)
        self.assertEqual(
            comparison["column_derivation_differences"],
            [{
                "column_key": "amount",
                "evidence_status": "CONFIRMED",
                "derivations_by_branch": {
                    "task:one": ["column:source-one"],
                    "task:two": ["column:source-two"],
                },
            }],
        )
        self.assertEqual(
            comparison["evidence_status"]["write_mode_by_branch"],
            {"task:one": "CONFIRMED", "task:two": "MISSING"},
        )
        self.assertEqual(comparison["evidence_status"]["expressions"], "UNSUPPORTED")
        self.assertFalse(result["data"]["truncated"])
        self.assertFalse(result["page"]["has_more"])
        self.assertEqual(result["page"]["returned"], 1)
        comparison_query = next(
            cypher for cypher, _, _ in store.calls if "branch_entity_ids" in cypher
        )
        self.assertIn("collect(DISTINCT", comparison_query)
        self.assertIn("DEPENDS_ON*0..2", comparison_query)
        self.assertIn(":READS", comparison_query)
        self.assertNotIn(":CONSUMES", comparison_query)
        self.assertNotIn("LIMIT", comparison_query)

    def test_graph_native_capabilities_advertise_only_the_implemented_operation(self):
        service = QueryService(
            CompareBranchesStore(),
            project_id="project-one",
        )

        result = service.get_graph_native_capabilities()

        self.assertEqual(result["provider_id"], "graph-query-provider")
        self.assertEqual(result["contract_version"], "graph-native:v1")
        self.assertEqual(result["supported_primitives"], ["compare_branches"])
        self.assertNotIn(
            "comparison_schema_version",
            result["primitive_capabilities"]["compare_branches"],
        )
        self.assertIn(
            "metric",
            result["primitive_capabilities"]["compare_branches"]["entity_layers"],
        )
        self.assertEqual(result["graph_context"]["build_id"], "build-1")
        self.assertEqual(
            set(result["graph_context"]),
            {"project_id", "build_id", "built_at", "is_latest", "has_pending_change"},
        )

    def test_compare_branches_rejects_scope_and_build_mismatches(self):
        service = QueryService(CompareBranchesStore())
        base = {
            "project_id": "project-one",
            "contract_version": "graph-native:v1",
            "graph_build_id": "build-1",
            "confirmed_branch_ids": ["task:one", "task:two"],
            "limit": 20,
        }

        for invalid in [
            {**base, "confirmed_branch_ids": ["task:one"]},
            {**base, "confirmed_branch_ids": ["task:one", "task:one"]},
            {**base, "confirmed_branch_ids": ["task:one", "dataset:two"]},
            {**base, "graph_build_id": "build-stale"},
        ]:
            result = service.execute("compare_branches", invalid)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["warnings"][0]["code"], "INVALID_REQUEST")

    def test_compare_branches_embeds_bounded_structural_summary(self):
        store = StructuralCompareBranchesStore()
        service = QueryService(store, project_id="project-one")

        result = service.execute("compare_branches", {
            "project_id": "project-one",
            "contract_version": "graph-native:v1",
            "graph_build_id": "build-1",
            "confirmed_branch_ids": ["task:0", "task:1", "task:2"],
            "max_depth": 2,
            "limit": 20,
        })

        self.assertEqual(result["status"], "ok")
        comparison = result["data"]["results"][0]
        group = comparison["partially_shared_entity_groups"][0]
        self.assertEqual(group["branch_ids"], ["task:0", "task:1"])
        self.assertEqual(
            group["structural_summary"]["observed_relationship_count"], 1
        )
        self.assertEqual(
            group["structural_summary"]["query_anchor_entity_ids"],
            ["dataset:shared-one", "dataset:shared-two"],
        )
        self.assertEqual(comparison["topology_scope"], {
            "max_depth": 2,
            "relationship_types": [
                "COMPUTED_BY", "DATASET_DEPENDS_ON", "DEPENDS_ON",
                "DERIVED_FROM", "EMITS_SQL", "HAS_COLUMN", "HAS_DEFINITION",
                "PRODUCES", "READS", "STORED_IN",
            ],
            "semantics": (
                "LITERAL_RELATIONSHIP_INSTANCES_IN_EXACT_MEMBERSHIP_"
                "INDUCED_SUBGRAPH"
            ),
            "evidence_status": "OBSERVED",
        })
        topology_calls = [
            call for call in store.calls
            if "compare_branches:structural_topology" in call[0]
        ]
        self.assertEqual(len(topology_calls), 1)
        self.assertEqual(
            topology_calls[0][1]["branch_scopes"],
            [
                {
                    "branch_id": "task:0",
                    "entity_ids": [
                        "dataset:shared-one", "dataset:shared-two",
                    ],
                },
                {
                    "branch_id": "task:1",
                    "entity_ids": [
                        "dataset:shared-one", "dataset:shared-two",
                    ],
                },
                {"branch_id": "task:2", "entity_ids": []},
            ],
        )

    def test_compare_branches_preserves_core_result_when_topology_fails(self):
        store = StructuralCompareBranchesStore(
            topology_error=RuntimeError("topology unavailable")
        )
        service = QueryService(store, project_id="project-one")

        result = service.execute("compare_branches", {
            "project_id": "project-one",
            "contract_version": "graph-native:v1",
            "graph_build_id": "build-1",
            "confirmed_branch_ids": ["task:0", "task:1", "task:2"],
            "limit": 20,
        })

        self.assertEqual(result["status"], "partial")
        comparison = result["data"]["results"][0]
        self.assertNotIn(
            "structural_summary",
            comparison["partially_shared_entity_groups"][0],
        )
        self.assertEqual(
            comparison["topology_scope"]["evidence_status"], "UNAVAILABLE"
        )
        self.assertIn(
            "STRUCTURAL_TOPOLOGY_UNAVAILABLE",
            [item["code"] for item in result["warnings"]],
        )

    def test_compare_branches_skips_topology_when_a_branch_is_unresolved(self):
        store = StructuralCompareBranchesStore(unresolved_branch_id="task:2")
        service = QueryService(store, project_id="project-one")

        result = service.execute("compare_branches", {
            "project_id": "project-one",
            "contract_version": "graph-native:v1",
            "graph_build_id": "build-1",
            "confirmed_branch_ids": ["task:0", "task:1", "task:2"],
            "limit": 20,
        })

        comparison = result["data"]["results"][0]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            comparison["topology_scope"]["evidence_status"], "UNAVAILABLE"
        )
        self.assertFalse(any(
            "compare_branches:structural_topology" in cypher
            for cypher, _, _ in store.calls
        ))

    def test_compare_branches_runs_small_scope_once_without_recovery(self):
        store = AdaptiveCompareBranchesStore()
        service = QueryService(store, project_id="project-one")

        result = service.execute(
            "compare_branches",
            {
                "project_id": "project-one",
                "contract_version": "graph-native:v1",
                "graph_build_id": "build-1",
                "confirmed_branch_ids": [f"task:{index}" for index in range(5)],
                "max_depth": 6,
                "limit": 100,
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(store.scope_batch_sizes, [5])
        self.assertEqual(result["data"]["execution"]["strategy"], "single_query")
        self.assertEqual(result["data"]["execution"]["attempted_batch_sizes"], [5])
        self.assertEqual(result["data"]["execution"]["applied_max_depth"], 6)
        self.assertTrue(result["data"]["execution"]["semantic_scope_preserved"])
        self.assertNotIn(
            "QUERY_RECOVERED_BY_BATCH_SPLIT",
            [item["code"] for item in result["warnings"]],
        )

    def test_compare_branches_recovers_from_memory_error_by_bisection(self):
        store = AdaptiveCompareBranchesStore(
            fail_above=5,
            error_code="Neo.TransientError.General.MemoryPoolOutOfMemoryError",
        )
        service = QueryService(store, project_id="project-one")
        branch_ids = [f"task:{index}" for index in range(10)]

        result = service.execute(
            "compare_branches",
            {
                "project_id": "project-one",
                "contract_version": "graph-native:v1",
                "graph_build_id": "build-1",
                "confirmed_branch_ids": branch_ids,
                "max_depth": 6,
                "limit": 100,
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(store.scope_batch_sizes, [10, 5, 5])
        self.assertTrue(all("DEPENDS_ON*0..6" in query for query in store.scope_queries))
        self.assertEqual(
            set(result["data"]["results"][0]["entity_sets_by_branch"]),
            set(branch_ids),
        )
        execution = result["data"]["execution"]
        self.assertEqual(execution["strategy"], "adaptive_bisection")
        self.assertEqual(execution["attempt_count"], 3)
        self.assertEqual(execution["memory_recovery_count"], 1)
        self.assertEqual(execution["requested_max_depth"], 6)
        self.assertEqual(execution["applied_max_depth"], 6)
        self.assertIn(
            "QUERY_RECOVERED_BY_BATCH_SPLIT",
            [item["code"] for item in result["warnings"]],
        )
        baseline_service = QueryService(
            AdaptiveCompareBranchesStore(),
            project_id="project-one",
        )
        baseline = baseline_service.execute(
            "compare_branches",
            {
                "project_id": "project-one",
                "contract_version": "graph-native:v1",
                "graph_build_id": "build-1",
                "confirmed_branch_ids": branch_ids,
                "max_depth": 6,
                "limit": 100,
            },
        )
        self.assertEqual(result["data"]["component_counts"], baseline["data"]["component_counts"])
        self.assertEqual(result["data"]["coverage"], baseline["data"]["coverage"])
        self.assertEqual(result["data"]["results"], baseline["data"]["results"])

    def test_compare_branches_bounds_large_physical_batches_without_changing_scope(self):
        store = AdaptiveCompareBranchesStore()
        service = QueryService(store, project_id="project-one")
        branch_ids = [f"task:{index}" for index in range(12)]

        result = service.execute(
            "compare_branches",
            {
                "project_id": "project-one",
                "contract_version": "graph-native:v1",
                "graph_build_id": "build-1",
                "confirmed_branch_ids": branch_ids,
                "max_depth": 6,
                "limit": 100,
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(store.scope_batch_sizes, [10, 2])
        self.assertEqual(result["data"]["execution"]["strategy"], "bounded_batches")
        self.assertEqual(
            set(result["data"]["results"][0]["entity_sets_by_branch"]),
            set(branch_ids),
        )

    def test_compare_branches_does_not_retry_non_memory_errors(self):
        store = AdaptiveCompareBranchesStore(
            fail_above=0,
            error_code="Neo.ClientError.Statement.SyntaxError",
        )
        service = QueryService(store, project_id="project-one")

        result = service.execute(
            "compare_branches",
            {
                "project_id": "project-one",
                "contract_version": "graph-native:v1",
                "graph_build_id": "build-1",
                "confirmed_branch_ids": ["task:one", "task:two"],
                "max_depth": 6,
                "limit": 100,
            },
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(store.scope_batch_sizes, [2])
        self.assertEqual(result["warnings"][0]["code"], "QUERY_EXECUTION_FAILED")

    def test_compare_branches_stops_when_one_branch_still_exceeds_memory(self):
        store = AdaptiveCompareBranchesStore(
            fail_above=0,
            error_code="Neo.TransientError.General.MemoryPoolOutOfMemoryError",
        )
        service = QueryService(store, project_id="project-one")

        result = service.execute(
            "compare_branches",
            {
                "project_id": "project-one",
                "contract_version": "graph-native:v1",
                "graph_build_id": "build-1",
                "confirmed_branch_ids": ["task:one", "task:two"],
                "max_depth": 6,
                "limit": 100,
            },
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(store.scope_batch_sizes, [2, 1])
        self.assertEqual(result["warnings"][0]["code"], "QUERY_EXECUTION_FAILED")

    def test_recent_changes_reads_incremental_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            changes_dir = project_dir / "incremental" / "changes"
            changes_dir.mkdir(parents=True)
            (project_dir / "incremental" / "state.json").write_text(
                json.dumps({"last_scan_at": "2026-07-28T10:00:00", "last_semantic_change": True}),
                encoding="utf-8",
            )
            (changes_dir / "20260728.json").write_text(
                json.dumps(
                    {
                        "project_id": "demo",
                        "semantic_change": True,
                        "code_changed_task_ids": ["1", "2"],
                        "affected_task_ids": ["3"],
                        "affected_metric_ids": ["m1"],
                    }
                ),
                encoding="utf-8",
            )

            service = QueryService(EmptyStore(), project_id="demo", project_dir=project_dir)
            result = service.execute("get_recent_changes", {"project_id": "demo"})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data"]["summary"]["event_count"], 1)
            self.assertEqual(result["data"]["summary"]["semantic_change_event_count"], 1)
            self.assertEqual(result["data"]["events"][0]["code_changed_task_count"], 2)
            self.assertEqual(result["data"]["events"][0]["affected_metric_ids_sample"], ["m1"])

    def test_impact_groups_respects_requested_hops(self):
        store = RecordingStore()
        service = QueryService(store)
        node = {"id": "column:a.x", "labels": ["Column"], "properties": {"name": "x"}}

        service._impact_groups(node, {"project_id": "demo", "max_hops": 3})

        self.assertIn("*1..3", store.queries[0][0])
        self.assertNotIn("*1..50", store.queries[0][0])

    def test_graph_neighborhood_returns_visual_graph(self):
        service = QueryService(NeighborhoodStore(), project_id="demo")
        result = service.execute(
            "get_graph_neighborhood",
            {
                "project_id": "demo",
                "subject": {"entity_id": "column:a.x"},
                "direction": "downstream",
                "relation_profile": "column_lineage",
                "max_hops": 2,
            },
        )

        self.assertEqual(result["status"], "ok")
        graph = result["data"]["visual_graph"]
        self.assertEqual([node["id"] for node in graph["nodes"]], ["column:a.x", "column:b.x"])
        self.assertEqual(graph["edges"][0]["source"], "column:a.x")
        self.assertEqual(graph["edges"][0]["target"], "column:b.x")
        self.assertEqual(graph["edges"][0]["graph_source"], "column:b.x")

    def test_graph_neighborhood_rejects_unsafe_edge_type(self):
        service = QueryService(NeighborhoodStore(), project_id="demo")
        result = service.execute(
            "get_graph_neighborhood",
            {
                "project_id": "demo",
                "subject": {"entity_id": "column:a.x"},
                "edge_types": ["OWNS"],
            },
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["warnings"][0]["code"], "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()
