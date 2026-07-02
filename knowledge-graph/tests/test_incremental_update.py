#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path

from incremental_update import (
    compare_snapshots,
    downstream_closure,
    mark_manual_overrides,
    normalize_sql,
    validate_refresh_quality,
)


class IncrementalUpdateTest(unittest.TestCase):
    def test_sql_formatting_has_same_semantics(self):
        compact = normalize_sql("select a, b from t where x = 1")
        formatted = normalize_sql("SELECT\n  a,\n  b\nFROM t\nWHERE x = 1")
        self.assertEqual(compact, formatted)

    def test_downstream_closure(self):
        edges = [["1", "2"], ["2", "3"], ["9", "10"]]
        self.assertEqual(downstream_closure({"1"}, edges), ["1", "2", "3"])

    def test_semantic_change_propagates_downstream(self):
        old = {
            "tasks": {
                "1": {"metadata_hash": "m", "code_raw_hash": "a", "code_semantic_hash": "a"},
                "2": {"metadata_hash": "m", "code_raw_hash": "b", "code_semantic_hash": "b"},
            },
            "edges": [["1", "2"]],
            "dataset_schema_hash": "s",
            "indicator_registry_hash": "i",
        }
        new = json.loads(json.dumps(old))
        new["tasks"]["1"]["code_raw_hash"] = "c"
        new["tasks"]["1"]["code_semantic_hash"] = "c"
        result = compare_snapshots(old, new)
        self.assertTrue(result["semantic_change"])
        self.assertEqual(result["code_changed_task_ids"], ["1"])
        self.assertEqual(result["affected_task_ids"], ["1", "2"])

    def test_text_only_change_does_not_rebuild(self):
        old = {
            "tasks": {"1": {"metadata_hash": "m", "code_raw_hash": "a", "code_semantic_hash": "same"}},
            "edges": [],
            "dataset_schema_hash": "s",
            "indicator_registry_hash": "i",
        }
        new = json.loads(json.dumps(old))
        new["tasks"]["1"]["code_raw_hash"] = "formatting"
        result = compare_snapshots(old, new)
        self.assertFalse(result["semantic_change"])
        self.assertEqual(result["text_only_changed_task_ids"], ["1"])

    def test_manual_override_is_marked_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            path = project_dir / "manual_metric_overrides.json"
            path.write_text(json.dumps([{"metric_id": "m1", "needs_review": False}, {"metric_id": "m2"}]))
            changed = mark_manual_overrides(project_dir, {"m1"}, "2026-06-30T00:00:00+08:00")
            rows = json.loads(path.read_text())
            self.assertEqual(changed, 1)
            self.assertTrue(rows[0]["needs_review"])
            self.assertNotIn("needs_review", rows[1])

    def test_refresh_quality_rejects_missing_hive_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            (project_dir / "lineage.json").write_text(json.dumps({"nodes": [{"task_id": "1"}], "errors": []}))
            (project_dir / "task_details.json").write_text(json.dumps([{"task_id": "1", "task_type": "hiveTask"}]))
            for name in ["task_detail_errors.json", "code_artifacts_page_errors.json", "log_collection_errors.json", "log_artifacts_full.json"]:
                (project_dir / name).write_text("[]")
            quality = validate_refresh_quality(project_dir)
            self.assertFalse(quality["ok"])
            self.assertEqual(quality["missing_hive_log_count"], 1)


if __name__ == "__main__":
    unittest.main()
