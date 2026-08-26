import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from kg_probe.input_pack_adapter import build_project


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def test_build_project_consumes_task_pack_sql(tmp_path: Path) -> None:
    input_root = tmp_path / "input-pack"
    task_dir = input_root / "tasks" / "hiveTask" / "1001"
    task_dir.mkdir(parents=True)
    (task_dir / "sql").mkdir()
    (task_dir / "sql" / "query.sql").write_text(
        "INSERT OVERWRITE TABLE result SELECT id FROM pdata.source;",
        encoding="utf-8",
    )
    write_json(
        task_dir / "task.json",
        {
            "schemaVersion": "1.0.0",
            "taskId": "1001",
            "taskCategory": "hiveTask",
            "taskType": "hiveTask",
            "taskName": "demo_task",
            "topicName": "DEMO",
            "target": {
                "platform": "hive",
                "qualifiedName": "dm.result",
                "dataSource": "gfhive",
            },
            "targetEvidenceKind": "DIRECT_PLATFORM_TARGET",
            "sqlFiles": [
                {
                    "slot": "query",
                    "path": "sql/query.sql",
                    "sha256": "a" * 64,
                    "evidenceProvider": "test",
                }
            ],
            "collectedAt": "2026-08-26T00:00:00Z",
            "contentHash": "b" * 64,
        },
    )

    output_dir = tmp_path / "project"
    summary = build_project(input_root, output_dir, task_ids=["1001"])

    assert summary["task_count"] == 1
    assert json.loads((output_dir / "lineage.json").read_text())["edges"] == []
    statements = json.loads((output_dir / "strategy_sql_statements.json").read_text())
    assert statements[0]["task_id"] == "1001"
    assert statements[0]["source_type"] == "canonical_task_sql"
    edges = json.loads((output_dir / "strategy_dataset_edges.json").read_text())
    assert {item["relation"] for item in edges} == {"READ_BY", "WRITES"}
    target_edge = next(item for item in edges if item["relation"] == "WRITES")
    assert target_edge["source_type"] == "input_pack_task_target"
