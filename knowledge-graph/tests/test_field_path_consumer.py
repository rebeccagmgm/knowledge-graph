import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from kg_probe.field_path_consumer import FieldPathConsumer


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, values) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(value, ensure_ascii=False) for value in values), encoding="utf-8")


def test_consumes_existing_facts_and_bridges_tasks(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "strategy_graph_edges.jsonl",
        [
            {"from": "task:100", "to": "dataset:dm.target", "type": "PRODUCES"},
            {"from": "task:200", "to": "dataset:pdata.middle", "type": "PRODUCES"},
        ],
    )
    write_json(
        tmp_path / "strategy_column_lineage.json",
        [
            {
                "task_id": "100",
                "target_dataset": "dm.target",
                "target_column": "entity_id",
                "source_dataset": "pdata.middle",
                "source_column": "agt_id",
                "source_resolution": "table_alias",
                "statement_id": "100_sql",
            },
            {
                "task_id": "200",
                "target_dataset": "pdata.middle",
                "target_column": "agt_id",
                "source_dataset": "titans.raw",
                "source_column": "entity_id",
                "source_resolution": "single_read_dataset",
                "statement_id": "200_sql",
            },
        ],
    )
    result = FieldPathConsumer(tmp_path).build("dm.target", ["entity_id"])

    assert result["status"] == "COMPLETE"
    path = result["paths"][0]
    assert [step["task_id"] for step in path["steps"]] == ["100", "200", None]
    assert [step["dataset"] for step in path["steps"]] == ["dm.target", "pdata.middle", "titans.raw"]
    assert path["links"][0]["status"] == "CONFIRMED"


def test_unresolved_fact_can_use_sql_projection_alias(tmp_path: Path) -> None:
    sql_path = tmp_path / "sql.sql"
    sql_path.write_text(
        "SELECT T.STATI_CONT_DESC AS INTERNAL_TRADE_ID FROM (SELECT * FROM PDATA.MIDDLE) T",
        encoding="utf-8",
    )
    write_jsonl(
        tmp_path / "strategy_graph_edges.jsonl",
        [
            {"from": "task:100", "to": "dataset:dm.target", "type": "PRODUCES"},
            {"from": "task:200", "to": "dataset:pdata.middle", "type": "PRODUCES"},
        ],
    )
    write_json(
        tmp_path / "strategy_column_lineage.json",
        [
            {
                "task_id": "100",
                "target_dataset": "dm.target",
                "target_column": "internal_trade_id",
                "source_dataset": "",
                "source_column": "internal_trade_id",
                "source_resolution": "unresolved_dataset",
                "statement_id": "100_sql",
            },
            {
                "task_id": "200",
                "target_dataset": "pdata.middle",
                "target_column": "stati_cont_desc",
                "source_dataset": "titans.raw",
                "source_column": "internal_trade_id",
                "source_resolution": "single_read_dataset",
                "statement_id": "200_sql",
            },
        ],
    )
    write_json(
        tmp_path / "strategy_sql_statements.json",
        [{"task_id": "100", "statement_id": "100_sql", "statement_path": str(sql_path)}],
    )
    result = FieldPathConsumer(tmp_path).build("dm.target", ["internal_trade_id"])

    assert result["paths"][0]["status"] == "CONFIRMED"
    assert result["paths"][0]["steps"][1]["dataset"] == "pdata.middle"
    assert result["paths"][0]["steps"][1]["field"] == "stati_cont_desc"


def test_matches_sql_dataset_to_task_output_with_scheduler_scope(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "strategy_graph_edges.jsonl",
        [
            {"from": "task:100", "to": "dataset:dm.target", "type": "PRODUCES"},
            {"from": "task:200", "to": "dataset:pdata.middle_tit165", "type": "PRODUCES"},
            {"from": "task:100", "to": "task:200", "type": "DEPENDS_ON"},
        ],
    )
    write_json(
        tmp_path / "strategy_column_lineage.json",
        [
            {
                "task_id": "100",
                "target_dataset": "dm.target",
                "target_column": "entity_id",
                "source_dataset": "pdata.middle",
                "source_column": "agt_id",
                "source_resolution": "table_alias",
                "statement_id": "100_sql",
            },
            {
                "task_id": "200",
                "target_dataset": "pdata.middle_tit165",
                "target_column": "agt_id",
                "source_dataset": "titans.raw",
                "source_column": "entity_id",
                "source_resolution": "single_read_dataset",
                "statement_id": "200_sql",
            },
        ],
    )
    result = FieldPathConsumer(tmp_path).build("dm.target", ["entity_id"])

    assert result["paths"][0]["status"] == "CONFIRMED"
    assert [step["task_id"] for step in result["paths"][0]["steps"]] == ["100", "200", None]
    assert result["paths"][0]["links"][0]["evidence"][-1]["kind"] == "KG_PRODUCES"
