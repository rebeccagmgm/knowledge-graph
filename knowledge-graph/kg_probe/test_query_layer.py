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


class RecordingStore:
    def __init__(self):
        self.queries = []

    def query(self, cypher, parameters=None):
        self.queries.append((cypher, parameters or {}))
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


if __name__ == "__main__":
    unittest.main()
