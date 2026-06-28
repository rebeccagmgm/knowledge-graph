#!/usr/bin/env python3
"""Extract conservative column-level lineage from SQL statements."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


LOCAL_DEPS = Path(os.environ.get("KG_LOCAL_PYDEPS", "vendor"))
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))

import sqlglot
from sqlglot import exp


def norm_dataset(value: str) -> str:
    return (value or "").lower()


def norm_column(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", (value or "").strip()).strip("_").lower()


def column_id(dataset: str, column: str) -> str:
    return f"column:{norm_dataset(dataset)}.{norm_column(column)}"


def table_name(table: exp.Table) -> str:
    db = table.args.get("db")
    catalog = table.args.get("catalog")
    parts = []
    if catalog:
        parts.append(catalog.name.lower())
    if db:
        parts.append(db.name.lower())
    parts.append(table.name.lower())
    return ".".join(part for part in parts if part)


def load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


NOISE_LINE_RE = re.compile(
    r"^(?:connecting to|connected to|finished |job information|number of |"
    r"hive session id|time taken|ok$|slf4j|warning:|spark context|"
    r"starting job|ended job|tracking url|kill command|executequery\b|"
    r"executing with spark engine|kerberos tgt|stage-\d+|map\s+\d+%|"
    r"reduce\s+\d+%|spark\.sql\.|hive\.)",
    re.IGNORECASE,
)
SQL_LINE_RE = re.compile(
    r"\b(?:select|with|insert|create|drop|truncate|alter|delete|from|join|where|group\s+by|order\s+by)\b",
    re.IGNORECASE,
)


def normalize_sql_for_parse(sql: str) -> str:
    sql = re.sub(r"\x1b\[[0-9;]*m", "", sql).replace("\r\n", "\n").replace("\r", "\n")
    sql = re.sub(r"^\s*(?:CREATE\s+TABLE\s+statement|SQL\s+statement)\s*:\s*", "", sql, flags=re.IGNORECASE)
    lines = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if NOISE_LINE_RE.search(line) and not SQL_LINE_RE.search(line):
            continue
        if re.match(r"^(?:set|add jar|use)\s+\S+", line, re.IGNORECASE):
            continue
        lines.append(raw_line)
    return "\n".join(lines).strip()


def graph_task_outputs(project_dir: Path, prefix: str) -> dict[str, set[str]]:
    path = project_dir / f"{prefix}_graph_edges.jsonl"
    outputs: dict[str, set[str]] = defaultdict(set)
    if not path.exists():
        return outputs
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        edge = json.loads(line)
        if edge.get("type") == "PRODUCES" and edge.get("from", "").startswith("task:"):
            outputs[edge["from"].removeprefix("task:")].add(edge["to"].removeprefix("dataset:"))
    return outputs


def statement_dataset_edges(project_dir: Path, prefix: str):
    edges = load(project_dir / f"{prefix}_dataset_edges.json", [])
    reads_by_stmt: dict[str, set[str]] = defaultdict(set)
    writes_by_stmt: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.get("relation") == "READ_BY":
            reads_by_stmt[edge["to"]].add(norm_dataset(edge["from"]))
        elif edge.get("relation") == "WRITES":
            writes_by_stmt[edge["from"]].add(norm_dataset(edge["to"]))
    return reads_by_stmt, writes_by_stmt


def dms_column_index(project_dir: Path) -> dict[str, list[str]]:
    path = project_dir / "sz_metadata" / "dataset_dms.json"
    records = load(path, [])
    index: dict[str, list[str]] = defaultdict(list)
    for item in records:
        dataset = norm_dataset(item.get("dataset", ""))
        exact_records = item.get("exact_records") or []
        if not dataset or not exact_records:
            continue
        columns = exact_records[0].get("refColumns") or []
        for column in columns:
            if isinstance(column, dict) and column.get("name"):
                column_name = norm_column(str(column["name"]))
                if column_name and column_name not in index[dataset]:
                    index[dataset].append(column_name)
    return index


def table_aliases(parsed: exp.Expression) -> dict[str, str]:
    aliases = {}
    cte_names = {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}
    for table in parsed.find_all(exp.Table):
        name = table_name(table)
        if name.split(".")[-1] in cte_names:
            continue
        alias = table.alias_or_name
        if alias:
            aliases[alias.lower()] = name
        aliases[name.split(".")[-1]] = name
    return aliases


def cte_names(parsed: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}


def select_expressions(parsed: exp.Expression) -> list[exp.Expression]:
    selects = list(parsed.find_all(exp.Select))
    if not selects:
        return []
    # The outermost SELECT is generally the statement output shape.
    return list(selects[0].expressions)


def output_name(expr: exp.Expression) -> str:
    alias = expr.alias_or_name
    if alias:
        return norm_column(alias)
    if isinstance(expr, exp.Column):
        return norm_column(expr.name)
    return ""


def resolve_direct_column(
    col_name: str,
    table: str,
    aliases: dict[str, str],
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
) -> dict:
    dataset = aliases.get(table, "")
    confidence = "table_alias"
    if not dataset and len(read_datasets) == 1:
        dataset = next(iter(read_datasets))
        confidence = "single_read_dataset"
    if not dataset and read_datasets and columns_by_dataset:
        matches = [
            dataset_name
            for dataset_name in read_datasets
            if col_name in set(columns_by_dataset.get(dataset_name, []))
        ]
        if len(matches) == 1:
            dataset = matches[0]
            confidence = "schema_unique_column"
    if not dataset:
        return {
            "dataset": "",
            "column": col_name,
            "column_id": f"column:unknown.{col_name}",
            "confidence": "unresolved_dataset",
        }
    return {
        "dataset": dataset,
        "column": col_name,
        "column_id": column_id(dataset, col_name),
        "confidence": confidence,
    }


def source_columns(
    expr: exp.Expression,
    aliases: dict[str, str],
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
    cte_column_sources: dict[str, dict[str, list[dict]]] | None = None,
) -> list[dict]:
    cte_column_sources = cte_column_sources or {}
    sources = []
    for col in expr.find_all(exp.Column):
        col_name = norm_column(col.name)
        if not col_name:
            continue
        table = (col.table or "").lower()
        if table in cte_column_sources and col_name in cte_column_sources[table]:
            for source in cte_column_sources[table][col_name]:
                sources.append({**source, "confidence": f"cte_{source['confidence']}"})
            continue
        sources.append(resolve_direct_column(col_name, table, aliases, read_datasets, columns_by_dataset))
    unique = {}
    for item in sources:
        unique[(item["dataset"], item["column"])] = item
    return list(unique.values())


def star_sources(
    expr: exp.Expression,
    aliases: dict[str, str],
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
    cte_column_sources: dict[str, dict[str, list[dict]]] | None = None,
) -> list[dict]:
    cte_column_sources = cte_column_sources or {}
    table = ""
    if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
        table = (expr.table or "").lower()
    elif isinstance(expr, exp.Star):
        table = ""
    else:
        return []

    datasets: list[str] = []
    if table:
        if table in cte_column_sources:
            expanded = []
            for column_name, sources in cte_column_sources[table].items():
                for source in sources:
                    expanded.append(
                        {
                            **source,
                            "output_column": column_name,
                            "confidence": f"cte_star_{source['confidence']}",
                        }
                    )
            return expanded
        dataset = aliases.get(table, "")
        if dataset:
            datasets = [dataset]
    else:
        for alias, dataset in aliases.items():
            if "." in dataset and dataset not in datasets:
                datasets.append(dataset)
        for dataset in sorted(read_datasets):
            if dataset not in datasets:
                datasets.append(dataset)

    expanded = []
    for dataset in datasets:
        for column_name in columns_by_dataset.get(dataset, []):
            expanded.append(
                {
                    "dataset": dataset,
                    "column": column_name,
                    "column_id": column_id(dataset, column_name),
                    "confidence": "schema_star_expand",
                    "output_column": column_name,
                }
            )
    return expanded


def projection_facts(
    projection: exp.Expression,
    aliases: dict[str, str],
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
    cte_column_sources: dict[str, dict[str, list[dict]]] | None = None,
) -> tuple[list[tuple[str, list[dict]]], str | None]:
    expanded = star_sources(projection, aliases, read_datasets, columns_by_dataset, cte_column_sources)
    if expanded:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for source in expanded:
            grouped[source.pop("output_column")].append(source)
        return sorted(grouped.items()), None

    out_col = output_name(projection)
    if not out_col:
        return [], "projection_without_output_name"
    sources = source_columns(projection, aliases, read_datasets, columns_by_dataset, cte_column_sources)
    if not sources:
        return [], "projection_without_source_column"
    return [(out_col, sources)], None


def build_cte_column_sources(
    parsed: exp.Expression,
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
) -> dict[str, dict[str, list[dict]]]:
    result: dict[str, dict[str, list[dict]]] = {}
    for cte in parsed.find_all(exp.CTE):
        cte_name = cte.alias_or_name.lower()
        select = cte.this.find(exp.Select) if cte.this else None
        if not select:
            continue
        aliases = table_aliases(cte.this)
        cte_map: dict[str, list[dict]] = {}
        for projection in select.expressions:
            items, error = projection_facts(projection, aliases, read_datasets, columns_by_dataset, result)
            if error:
                continue
            for out_col, sources in items:
                cte_map[out_col] = sources
        if cte_map:
            result[cte_name] = cte_map
    return result


def extract_for_statement(
    stmt: dict,
    reads_by_stmt: dict[str, set[str]],
    writes_by_stmt: dict[str, set[str]],
    outputs_by_task: dict[str, set[str]],
    columns_by_dataset: dict[str, set[str]],
    dialect: str,
) -> tuple[list[dict], list[dict]]:
    statement_id = stmt["statement_id"]
    task_id = stmt["task_id"]
    statement_path = Path(stmt["statement_path"])
    if not statement_path.exists():
        return [], [{"statement_id": statement_id, "task_id": task_id, "error": "missing_statement_file"}]

    target_datasets = set(writes_by_stmt.get(statement_id, set()))
    target_source = "statement_write"
    if not target_datasets:
        produced = outputs_by_task.get(task_id, set())
        if len(produced) == 1:
            target_datasets = set(produced)
            target_source = "task_single_produces"
        elif len(produced) > 1:
            return [], [
                {
                    "statement_id": statement_id,
                    "task_id": task_id,
                    "error": "ambiguous_task_outputs",
                    "target_count": len(produced),
                }
            ]
        else:
            return [], [{"statement_id": statement_id, "task_id": task_id, "error": "missing_target_dataset"}]

    sql = normalize_sql_for_parse(statement_path.read_text(errors="ignore"))
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parsed = sqlglot.parse_one(sql, read=dialect, error_level="ignore")
    except Exception as exc:  # noqa: BLE001
        return [], [{"statement_id": statement_id, "task_id": task_id, "error": str(exc)}]
    if parsed is None:
        return [], [{"statement_id": statement_id, "task_id": task_id, "error": "empty_parse"}]

    projections = select_expressions(parsed)
    if not projections:
        return [], [{"statement_id": statement_id, "task_id": task_id, "error": "no_select_projection"}]

    aliases = table_aliases(parsed)
    read_datasets = reads_by_stmt.get(statement_id, set())
    cte_column_sources = build_cte_column_sources(parsed, read_datasets, columns_by_dataset)
    facts = []
    errors = []
    for target_dataset in sorted(target_datasets):
        for ordinal, projection in enumerate(projections, start=1):
            projected_items, projection_error = projection_facts(
                projection,
                aliases,
                read_datasets,
                columns_by_dataset,
                cte_column_sources,
            )
            if projection_error:
                errors.append(
                    {
                        "statement_id": statement_id,
                        "task_id": task_id,
                        "error": projection_error,
                        "target_dataset": target_dataset,
                        "projection_ordinal": ordinal,
                    }
                )
                continue
            for out_col, sources in projected_items:
                for source in sources:
                    facts.append(
                        {
                            "statement_id": statement_id,
                            "task_id": task_id,
                            "target_dataset": target_dataset,
                            "target_column": out_col,
                            "target_column_id": column_id(target_dataset, out_col),
                            "source_dataset": source["dataset"],
                            "source_column": source["column"],
                            "source_column_id": source["column_id"],
                            "source_resolution": source["confidence"],
                            "target_resolution": target_source,
                            "projection_ordinal": ordinal,
                            "expression_sql": projection.sql(dialect=dialect)[:1000],
                            "source_system": "sqlglot",
                            "source_type": "column_lineage",
                        }
                    )
    return facts, errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--dialect", default="spark")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    statements = load(project_dir / f"{args.prefix}_sql_statements.json", [])
    if args.limit:
        statements = statements[: args.limit]
    reads_by_stmt, writes_by_stmt = statement_dataset_edges(project_dir, args.prefix)
    outputs_by_task = graph_task_outputs(project_dir, args.prefix)
    columns_by_dataset = dms_column_index(project_dir)

    all_facts = []
    all_errors = []
    for stmt in statements:
        facts, errors = extract_for_statement(
            stmt,
            reads_by_stmt,
            writes_by_stmt,
            outputs_by_task,
            columns_by_dataset,
            args.dialect,
        )
        all_facts.extend(facts)
        all_errors.extend(errors)

    facts_path = project_dir / f"{args.prefix}_column_lineage.json"
    errors_path = project_dir / f"{args.prefix}_column_lineage_errors.json"
    facts_path.write_text(json.dumps(all_facts, ensure_ascii=False, indent=2))
    errors_path.write_text(json.dumps(all_errors, ensure_ascii=False, indent=2))
    summary = {
        "statement_count": len(statements),
        "column_lineage_count": len(all_facts),
        "error_count": len(all_errors),
        "target_dataset_count": len({item["target_dataset"] for item in all_facts}),
        "source_dataset_count": len({item["source_dataset"] for item in all_facts if item["source_dataset"]}),
        "source_resolution_distribution": dict(Counter(item["source_resolution"] for item in all_facts)),
        "target_resolution_distribution": dict(Counter(item["target_resolution"] for item in all_facts)),
        "facts_path": str(facts_path),
        "errors_path": str(errors_path),
    }
    (project_dir / f"{args.prefix}_column_lineage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
