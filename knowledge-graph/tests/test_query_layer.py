#!/usr/bin/env python3

import unittest

from query_layer.contracts import response, validate_common
from query_layer.service import QueryService, entity_ref


class EmptyStore:
    def query(self, cypher, parameters=None):
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


if __name__ == "__main__":
    unittest.main()
