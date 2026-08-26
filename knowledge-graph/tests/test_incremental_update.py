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
    refresh_inputs,
    semantic_sql_for_artifact,
    validate_refresh_quality,
)
from collect_project import collect_upstream_graph


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, data):
        self.text = json.dumps({"result": True, "data": data})


class FakeHoraeApi:
    def __init__(self, upstream_by_task):
        self.upstream_by_task = upstream_by_task
        self.calls = []

    def get_downstream_list(self, task_id, is_downstream=0, hierarchy=1):
        self.calls.append(str(task_id))
        return FakeResponse(self.upstream_by_task.get(str(task_id), []))


class IncrementalUpdateTest(unittest.TestCase):
    def test_sql_formatting_has_same_semantics(self):
        compact = normalize_sql("select a, b from t where x = 1")
        formatted = normalize_sql("SELECT\n  a,\n  b\nFROM t\nWHERE x = 1")
        self.assertEqual(compact, formatted)

    def test_runtime_sql_dates_do_not_change_semantics(self):
        old = """
        INSERT OVERWRITE TABLE T PARTITION(BUSI_DATE = '2026-06-24')
        SELECT '2026-06-24' AS DATA_ETL_DATE, '2026-06-25 01:39:43' AS DATA_TIME
        FROM SRC WHERE BUSI_DATE = '2026-06-24'
        """
        new = """
        INSERT OVERWRITE TABLE T PARTITION(BUSI_DATE = '2026-08-20')
        SELECT '2026-08-20' AS DATA_ETL_DATE, '2026-08-21 01:41:00' AS DATA_TIME
        FROM SRC WHERE BUSI_DATE = '2026-08-20'
        """
        self.assertEqual(
            semantic_sql_for_artifact(old, "runtime_log"),
            semantic_sql_for_artifact(new, "runtime_log"),
        )

    def test_page_sql_keeps_literal_date_semantics(self):
        old = "SELECT * FROM SRC WHERE BUSI_DATE = '2026-06-24'"
        new = "SELECT * FROM SRC WHERE BUSI_DATE = '2026-08-20'"
        self.assertNotEqual(
            semantic_sql_for_artifact(old, "task_page"),
            semantic_sql_for_artifact(new, "task_page"),
        )

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

    def test_lineage_refresh_is_forced_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            steps = refresh_inputs(
                {"project_id": "p", "result_task_ids": ["1"], "supplemental_task_ids": [], "options": {}},
                project_dir,
                Path(tmp) / "out",
                Path(tmp) / "lineage",
                dry_run=True,
            )
            self.assertIn("--force", steps[0]["cmd"])

    def test_lineage_refresh_can_reuse_existing_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            steps = refresh_inputs(
                {
                    "project_id": "p",
                    "result_task_ids": ["1"],
                    "supplemental_task_ids": [],
                    "options": {"force_lineage_refresh": False},
                },
                project_dir,
                Path(tmp) / "out",
                Path(tmp) / "lineage",
                dry_run=True,
            )
            self.assertNotIn("--force", steps[0]["cmd"])

    def test_lineage_relation_cache_reuses_direct_upstream_calls(self):
        api = FakeHoraeApi({"1": [{"task_id": "2", "task_name": "pdata.x"}], "2": []})
        cache = {}
        collect_upstream_graph(api, "1", max_depth=5, max_nodes=10, sleep_sec=0, relation_cache=cache)
        collect_upstream_graph(api, "1", max_depth=5, max_nodes=10, sleep_sec=0, relation_cache=cache)
        self.assertEqual(api.calls, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
