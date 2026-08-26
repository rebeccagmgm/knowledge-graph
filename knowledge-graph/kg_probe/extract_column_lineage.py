#!/usr/bin/env python3
"""Extract conservative column-level lineage from SQL statements."""

from __future__ import annotations

import argparse
import contextlib
import html
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


LOCAL_DEPS = Path("/Applications/personal-work/kg-local-pydeps")
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))

import sqlglot
from sqlglot import exp


def norm_dataset(value: str) -> str:
    return (value or "").lower()


def norm_column(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()


def column_id(dataset: str, column: str) -> str:
    return f"column:{norm_dataset(dataset)}.{norm_column(column)}"


def is_dataset_name(value: str) -> bool:
    return bool(value and "." in value and not value.strip().startswith("${"))


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


def best_dataset_match(dataset: str, columns_by_dataset: dict[str, list[str]]) -> str:
    dataset = norm_dataset(dataset)
    if dataset in columns_by_dataset:
        return dataset
    if not dataset:
        return ""
    suffix = f".{dataset.split('.')[-1]}"
    matches = [name for name, columns in columns_by_dataset.items() if columns and name.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return dataset


def load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def resolve_statement_path(project_dir: Path, statement_path: str, statement_id: str) -> Path:
    path = Path(statement_path)
    if path.exists():
        return path
    local_path = project_dir / "sql" / f"{statement_id}.sql"
    if local_path.exists():
        return local_path
    return path


NOISE_LINE_RE = re.compile(
    r"^(?:connecting to|connected to|finished |job information|number of |"
    r"hive session id|time taken|ok$|slf4j|warning:|spark context|"
    r"starting job|ended job|tracking url|kill command|executequery\b|"
    r"executing with spark engine|kerberos tgt|stage-\d+|map\s+\d+%|"
    r"reduce\s+\d+%|spark\.sql\.|hive\.|total jobs|launching job|"
    r"ended job|counters:|mapreduce jobs launched|hdfs read|hdfs write|"
    r"records read|records written|bytes read|bytes written|"
    r"^\s*(?:finished|running|pending|failed)\s+\d+\s+\d+)",
    re.IGNORECASE,
)
SQL_LINE_RE = re.compile(
    r"\b(?:select|with|insert|create|drop|truncate|alter|delete|from|join|where|group\s+by|order\s+by)\b",
    re.IGNORECASE,
)


def normalize_sql_for_parse(sql: str) -> str:
    sql = re.sub(r"\x1b\[[0-9;]*m", "", sql).replace("\r\n", "\n").replace("\r", "\n")
    sql = sql.replace(r"\`", "`")
    sql = re.sub(
        r"^\s*(?:CREATE\s+TABLE\s+statement|INSERT\s+OVERWRITE\s+statement|SQL\s+statement)\s*:\s*",
        "",
        sql,
        flags=re.IGNORECASE,
    )
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


def ctas_select_sql(sql: str) -> str:
    match = re.search(r"\bAS\s+(SELECT|WITH)\b", sql, flags=re.IGNORECASE)
    if not match:
        return ""
    return sql[match.start(1) :].strip().rstrip(";")


def parse_sql(sql: str, dialect: str) -> exp.Expression | None:
    with contextlib.redirect_stderr(io.StringIO()):
        parsed = sqlglot.parse_one(sql, read=dialect, error_level="ignore")
    if parsed is not None and list(select_queries(parsed)):
        return parsed
    fallback = ctas_select_sql(sql)
    if not fallback:
        return parsed
    with contextlib.redirect_stderr(io.StringIO()):
        fallback_parsed = sqlglot.parse_one(fallback, read=dialect, error_level="ignore")
    return fallback_parsed or parsed


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


def hive_to_external_io(detail: dict) -> tuple[str, str]:
    task_type = detail.get("task_type", "")
    if not str(task_type).startswith("hive2"):
        return "", ""
    sync_info = detail.get("sync_info") or {}
    target = norm_dataset((sync_info.get("目标库表") or "").strip())
    hive_db = (sync_info.get("Hive源库") or "").strip()
    hive_table = (sync_info.get("Hive源表") or "").strip()
    source = norm_dataset(f"{hive_db}.{hive_table}") if hive_db and hive_table else ""
    if not is_dataset_name(target):
        target = ""
    if not is_dataset_name(source):
        source = ""
    return source, target


def table_aliases(parsed: exp.Expression) -> dict[str, str]:
    aliases = {}
    cte_names = {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}
    for table in parsed.find_all(exp.Table):
        name = table_name(table)
        if name.split(".")[-1] in cte_names:
            alias = table.alias_or_name
            cte_name = name.split(".")[-1]
            if alias:
                aliases[alias.lower()] = cte_name
            aliases[cte_name] = cte_name
            continue
        alias = table.alias_or_name
        if alias:
            aliases[alias.lower()] = name
        aliases[name.split(".")[-1]] = name
    for subquery in parsed.find_all(exp.Subquery):
        alias = subquery.alias_or_name
        if not alias:
            continue
        inner_tables = [
            table_name(table)
            for table in subquery.find_all(exp.Table)
            if table_name(table).split(".")[-1] not in cte_names
        ]
        unique_tables = []
        for name in inner_tables:
            if name not in unique_tables:
                unique_tables.append(name)
        if len(unique_tables) == 1:
            aliases[alias.lower()] = unique_tables[0]
        else:
            aliases[alias.lower()] = alias.lower()
    return aliases


def cte_names(parsed: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}


def select_queries(parsed: exp.Expression):
    if parsed is None:
        return []
    if isinstance(parsed, exp.Create):
        return select_queries(parsed.args.get("expression"))
    if isinstance(parsed, exp.Subquery):
        return select_queries(parsed.this)
    if isinstance(parsed, exp.Union):
        return select_queries(parsed.this) + select_queries(parsed.expression)
    if isinstance(parsed, exp.Select):
        return [parsed]
    selects = list(parsed.find_all(exp.Select))
    return selects[:1]


def select_expression_groups(parsed: exp.Expression) -> list[list[exp.Expression]]:
    return [list(select.expressions) for select in select_queries(parsed) if select.expressions]


def select_groups(parsed: exp.Expression) -> list[exp.Select]:
    return [select for select in select_queries(parsed) if select.expressions]


def output_name(expr: exp.Expression) -> str:
    alias = expr.alias_or_name
    if alias:
        return norm_column(alias)
    if isinstance(expr, exp.Column):
        return norm_column(expr.name)
    return ""


def nearest_select(expr: exp.Expression) -> exp.Select | None:
    node = getattr(expr, "parent", None)
    while node is not None:
        if isinstance(node, exp.Select):
            return node
        node = getattr(node, "parent", None)
    return None


def relation_aliases(select: exp.Select | None) -> list[str]:
    if select is None:
        return []
    aliases = []
    from_expr = select.args.get("from_")
    relations = []
    if from_expr is not None and isinstance(from_expr.this, exp.Expression):
        relations.append(from_expr.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Expression):
            relations.append(join.this)
    for relation in relations:
        alias = relation.alias_or_name
        if alias:
            value = alias.lower()
            if value not in aliases:
                aliases.append(value)
    return aliases


def expression_kind(expr: exp.Expression) -> str:
    if isinstance(expr, exp.Alias):
        return expression_kind(expr.this)
    if isinstance(expr, exp.Literal):
        return "literal"
    if isinstance(expr, exp.Null):
        return "literal"
    if isinstance(expr, (exp.CurrentDate, exp.CurrentTimestamp)):
        return "system_expression"
    if list(expr.find_all(exp.Column)):
        return "source_expression"
    return "generated_expression"


def prefixed_confidence(prefix: str, confidence: object) -> str:
    value = str(confidence or "unknown")
    for known_prefix in ("cte_star_", "cte_"):
        while value.startswith(known_prefix):
            value = value[len(known_prefix) :]
    return f"{prefix}{value}"


def resolve_direct_column(
    col_name: str,
    table: str,
    aliases: dict[str, str],
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
) -> dict:
    dataset = aliases.get(table, "")
    if dataset:
        dataset = best_dataset_match(dataset, columns_by_dataset)
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
        cte_key = ""
        if table in cte_column_sources:
            cte_key = table
        elif table and aliases.get(table, "") in cte_column_sources:
            cte_key = aliases[table]
        elif not table:
            select_aliases = relation_aliases(nearest_select(col))
            scoped_candidates = []
            for alias in select_aliases:
                alias_value = aliases.get(alias, alias)
                if alias_value in cte_column_sources and col_name in cte_column_sources[alias_value]:
                    scoped_candidates.append(alias_value)
            if len(scoped_candidates) == 1:
                cte_key = scoped_candidates[0]
            if not cte_key:
                physical_candidates = []
                for alias in select_aliases:
                    dataset = aliases.get(alias, "")
                    if "." in dataset:
                        dataset = best_dataset_match(dataset, columns_by_dataset)
                        if col_name in set(columns_by_dataset.get(dataset, [])):
                            physical_candidates.append(dataset)
                unique_physical = []
                for candidate in physical_candidates:
                    if candidate not in unique_physical:
                        unique_physical.append(candidate)
                if len(unique_physical) == 1:
                    dataset = unique_physical[0]
                    sources.append(
                        {
                            "dataset": dataset,
                            "column": col_name,
                            "column_id": column_id(dataset, col_name),
                            "confidence": "scoped_single_relation",
                        }
                    )
                    continue
        if not cte_key and not table:
            cte_candidates = []
            for alias_value in aliases.values():
                if alias_value in cte_column_sources and col_name in cte_column_sources[alias_value]:
                    cte_candidates.append(alias_value)
            unique_candidates = []
            for candidate in cte_candidates:
                if candidate not in unique_candidates:
                    unique_candidates.append(candidate)
            if len(unique_candidates) == 1:
                cte_key = unique_candidates[0]
            elif not unique_candidates:
                direct_candidates = [
                    name
                    for name, column_map in cte_column_sources.items()
                    if col_name in column_map
                ]
                if len(direct_candidates) == 1:
                    cte_key = direct_candidates[0]
        if cte_key and col_name in cte_column_sources[cte_key]:
            for source in cte_column_sources[cte_key][col_name]:
                sources.append({**source, "confidence": prefixed_confidence("cte_", source["confidence"])})
            continue
        sources.append(resolve_direct_column(col_name, table, aliases, read_datasets, columns_by_dataset))
    unique = {}
    for item in sources:
        unique[(item["dataset"], item["column"])] = item
    return list(unique.values())


def context_sources(
    expr: exp.Expression | None,
    influence_type: str,
    aliases: dict[str, str],
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
    cte_column_sources: dict[str, dict[str, list[dict]]] | None = None,
) -> list[dict]:
    if expr is None:
        return []
    result = []
    for source in source_columns(expr, aliases, read_datasets, columns_by_dataset, cte_column_sources):
        if source.get("dataset") and source.get("column"):
            result.append(
                {
                    **source,
                    "influence_type": influence_type,
                    "expression_sql": expr.sql()[:1000],
                }
            )
    return result


def select_context_sources(
    select: exp.Select,
    aliases: dict[str, str],
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
    cte_column_sources: dict[str, dict[str, list[dict]]] | None = None,
) -> list[dict]:
    sources = []
    for influence_type, expr in {
        "filter": select.args.get("where"),
        "group_by": select.args.get("group"),
        "having": select.args.get("having"),
        "qualify": select.args.get("qualify"),
        "order_by": select.args.get("order"),
    }.items():
        sources.extend(
            context_sources(expr, influence_type, aliases, read_datasets, columns_by_dataset, cte_column_sources)
        )
    for join in select.args.get("joins") or []:
        sources.extend(
            context_sources(
                join.args.get("on"),
                "join_condition",
                aliases,
                read_datasets,
                columns_by_dataset,
                cte_column_sources,
            )
        )
        using_expr = join.args.get("using")
        if isinstance(using_expr, list):
            for item in using_expr:
                if isinstance(item, exp.Expression):
                    sources.extend(
                        context_sources(
                            item,
                            "join_using",
                            aliases,
                            read_datasets,
                            columns_by_dataset,
                            cte_column_sources,
                        )
                    )
        elif isinstance(using_expr, exp.Expression):
            sources.extend(
                context_sources(
                    using_expr,
                    "join_using",
                    aliases,
                    read_datasets,
                    columns_by_dataset,
                    cte_column_sources,
                )
            )
    unique = {}
    for item in sources:
        unique[(item["dataset"], item["column"], item["influence_type"])] = item
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
        cte_key = table if table in cte_column_sources else aliases.get(table, "")
        if cte_key in cte_column_sources:
            expanded = []
            for column_name, sources in cte_column_sources[cte_key].items():
                for source in sources:
                    expanded.append(
                        {
                            **source,
                            "output_column": column_name,
                            "confidence": prefixed_confidence("cte_star_", source["confidence"]),
                        }
                    )
            return expanded
        dataset = aliases.get(table, "")
        if dataset:
            datasets = [best_dataset_match(dataset, columns_by_dataset)]
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
    fallback_output_name: str = "",
) -> tuple[list[tuple[str, list[dict]]], dict | None]:
    expanded = star_sources(projection, aliases, read_datasets, columns_by_dataset, cte_column_sources)
    if expanded:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for source in expanded:
            grouped[source.pop("output_column")].append(source)
        return sorted(grouped.items()), None

    out_col = output_name(projection) or norm_column(fallback_output_name)
    output_confidence = "target_schema_ordinal" if fallback_output_name and not output_name(projection) else ""
    if not out_col:
        return [], {"error": "projection_without_output_name"}
    sources = source_columns(projection, aliases, read_datasets, columns_by_dataset, cte_column_sources)
    if not sources:
        kind = expression_kind(projection)
        expression_sql = projection.sql()[:1000]
        if kind in {"literal", "system_expression", "generated_expression"}:
            return [
                (
                    out_col,
                    [
                        {
                            "dataset": "",
                            "column": "",
                            "column_id": "",
                            "confidence": kind,
                            "generation_type": kind,
                            "expression_sql": expression_sql,
                            "output_resolution": output_confidence,
                        }
                    ],
                )
            ], None
        return [], {
            "error": "projection_without_resolved_source_column",
            "generation_type": kind,
            "expression_sql": expression_sql,
        }
    if output_confidence:
        sources = [{**source, "output_resolution": output_confidence} for source in sources]
    return [(out_col, sources)], None


def build_cte_column_sources(
    parsed: exp.Expression,
    read_datasets: set[str],
    columns_by_dataset: dict[str, list[str]],
) -> dict[str, dict[str, list[dict]]]:
    result: dict[str, dict[str, list[dict]]] = {}
    for _ in range(4):
        changed = False
        for container_name, container_expr in [
            *[(cte.alias_or_name.lower(), cte.this) for cte in parsed.find_all(exp.CTE) if cte.alias_or_name],
            *[
                (subquery.alias_or_name.lower(), subquery.this)
                for subquery in parsed.find_all(exp.Subquery)
                if subquery.alias_or_name
            ],
        ]:
            selects = select_queries(container_expr) if container_expr else []
            if not selects:
                continue
            aliases = table_aliases(container_expr)
            column_map: dict[str, list[dict]] = {}
            for select in selects:
                for projection in select.expressions:
                    items, error = projection_facts(projection, aliases, read_datasets, columns_by_dataset, result)
                    if error:
                        continue
                    for out_col, sources in items:
                        existing = column_map.setdefault(out_col, [])
                        seen = {(item.get("dataset"), item.get("column"), item.get("confidence")) for item in existing}
                        for source in sources:
                            key = (source.get("dataset"), source.get("column"), source.get("confidence"))
                            if key not in seen:
                                existing.append(source)
                                seen.add(key)
            if column_map and column_map != result.get(container_name):
                result[container_name] = column_map
                changed = True
        if not changed:
            break
    return result


def create_table_name(sql: str) -> str:
    match = re.search(
        r"\bCREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)",
        sql,
        flags=re.IGNORECASE,
    )
    return norm_dataset(match.group(1)) if match else ""


def is_non_lineage_statement(sql: str) -> tuple[bool, str]:
    compact = sql.strip().rstrip(";")
    if not compact:
        return True, "empty_statement"
    lowered = compact.lower()
    if re.match(r"^(?:drop|truncate|alter|set|use|add\s+jar)\b", lowered):
        return True, "non_lineage_statement"
    if re.match(r"^create\s+(?:temporary\s+)?table\b", lowered) and not re.search(
        r"\bas\s+(?:select|with)\b", lowered
    ):
        return True, "create_table_ddl"
    return False, ""


def write_table_name(parsed: exp.Expression | None, sql: str, columns_by_dataset: dict[str, list[str]]) -> str:
    if isinstance(parsed, exp.Insert) and isinstance(parsed.this, exp.Table):
        return best_dataset_match(table_name(parsed.this), columns_by_dataset)
    match = re.search(
        r"\bINSERT\s+(?:OVERWRITE|INTO)\s+TABLE\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)",
        sql,
        flags=re.IGNORECASE,
    )
    if match:
        return best_dataset_match(norm_dataset(match.group(1)), columns_by_dataset)
    return ""


def infer_projection_output_columns(parsed: exp.Expression) -> list[str]:
    groups = select_expression_groups(parsed)
    if not groups:
        return []
    columns = []
    for projection in groups[0]:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            continue
        name = output_name(projection)
        if name and name not in columns:
            columns.append(name)
    return columns


def infer_ctas_columns(project_dir: Path, statements: list[dict], dialect: str) -> dict[str, list[str]]:
    inferred: dict[str, list[str]] = {}
    for stmt in statements:
        statement_id = stmt.get("statement_id", "")
        statement_path = resolve_statement_path(project_dir, stmt.get("statement_path", ""), statement_id)
        if not statement_path.exists():
            continue
        sql = normalize_sql_for_parse(statement_path.read_text(errors="ignore"))
        target = create_table_name(sql)
        if not target or target in inferred:
            continue
        try:
            parsed = parse_sql(sql, dialect)
        except Exception:
            continue
        if parsed is None:
            continue
        columns = infer_projection_output_columns(parsed)
        if columns:
            inferred[target] = columns
    return inferred


def extract_for_statement(
    project_dir: Path,
    stmt: dict,
    details_by_task: dict[str, dict],
    reads_by_stmt: dict[str, set[str]],
    writes_by_stmt: dict[str, set[str]],
    outputs_by_task: dict[str, set[str]],
    columns_by_dataset: dict[str, set[str]],
    dialect: str,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    statement_id = stmt["statement_id"]
    task_id = stmt["task_id"]
    statement_path = resolve_statement_path(project_dir, stmt.get("statement_path", ""), statement_id)
    if not statement_path.exists():
        return [], [], [{"statement_id": statement_id, "task_id": task_id, "error": "missing_statement_file"}], []

    sql = normalize_sql_for_parse(statement_path.read_text(errors="ignore"))
    should_skip, skip_reason = is_non_lineage_statement(sql)
    if should_skip:
        return [], [], [], [{"statement_id": statement_id, "task_id": task_id, "reason": skip_reason}]
    target_datasets = set(writes_by_stmt.get(statement_id, set()))
    target_source = "statement_write"
    sync_source_dataset, sync_target_dataset = hive_to_external_io(details_by_task.get(task_id, {}))
    if sync_target_dataset:
        target_datasets = {sync_target_dataset}
        target_source = "task_sync_target"
    ctas_target = create_table_name(sql)
    if ctas_target and not sync_target_dataset:
        target_datasets = {ctas_target}
        target_source = "ctas_target"
    if not target_datasets:
        produced = outputs_by_task.get(task_id, set())
        if len(produced) == 1:
            target_datasets = set(produced)
            target_source = "task_single_produces"
        elif len(produced) > 1:
            return [], [], [
                {
                    "statement_id": statement_id,
                    "task_id": task_id,
                    "error": "ambiguous_task_outputs",
                    "target_count": len(produced),
                }
            ], []
        else:
            target = write_table_name(None, sql, columns_by_dataset)
            if target:
                target_datasets = {target}
                target_source = "parsed_write_target"
            else:
                return [], [], [{"statement_id": statement_id, "task_id": task_id, "error": "missing_target_dataset"}], []

    try:
        parsed = parse_sql(sql, dialect)
    except Exception as exc:  # noqa: BLE001
        return [], [], [{"statement_id": statement_id, "task_id": task_id, "error": str(exc)}], []
    if parsed is None:
        return [], [], [{"statement_id": statement_id, "task_id": task_id, "error": "empty_parse"}], []

    if not target_datasets:
        target = write_table_name(parsed, sql, columns_by_dataset)
        if target:
            target_datasets = {target}
            target_source = "parsed_write_target"

    projection_groups = select_expression_groups(parsed)
    if not projection_groups:
        return [], [], [], [{"statement_id": statement_id, "task_id": task_id, "reason": "no_select_projection"}]

    aliases = table_aliases(parsed)
    read_datasets = set(reads_by_stmt.get(statement_id, set()))
    if sync_source_dataset:
        read_datasets.add(sync_source_dataset)
    cte_column_sources = build_cte_column_sources(parsed, read_datasets, columns_by_dataset)
    selects = select_groups(parsed)
    facts = []
    influence_facts = []
    errors = []
    for target_dataset in sorted(target_datasets):
        target_columns = columns_by_dataset.get(target_dataset, [])
        for branch_ordinal, projections in enumerate(projection_groups, start=1):
            output_columns = []
            for ordinal, projection in enumerate(projections, start=1):
                fallback_output_name = ""
                if not (
                    isinstance(projection, exp.Star)
                    or (isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star))
                ):
                    fallback_output_name = target_columns[ordinal - 1] if ordinal <= len(target_columns) else ""
                projected_items, projection_error = projection_facts(
                    projection,
                    aliases,
                    read_datasets,
                    columns_by_dataset,
                    cte_column_sources,
                    fallback_output_name=fallback_output_name,
                )
                if projection_error:
                    errors.append(
                        {
                            "statement_id": statement_id,
                            "task_id": task_id,
                            "error": projection_error["error"],
                            "target_dataset": target_dataset,
                            "projection_ordinal": ordinal,
                            "branch_ordinal": branch_ordinal,
                            "generation_type": projection_error.get("generation_type"),
                            "expression_sql": projection_error.get("expression_sql"),
                        }
                    )
                    continue
                for out_col, sources in projected_items:
                    if out_col and out_col not in output_columns:
                        output_columns.append(out_col)
                    for source in sources:
                        generation_type = source.get("generation_type")
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
                                "branch_ordinal": branch_ordinal,
                                "expression_sql": source.get("expression_sql") or projection.sql(dialect=dialect)[:1000],
                                "generation_type": generation_type,
                                "output_resolution": source.get("output_resolution", ""),
                                "source_system": "sqlglot",
                                "source_type": "generated_column" if generation_type else "column_lineage",
                            }
                        )
            if branch_ordinal <= len(selects) and output_columns:
                for source in select_context_sources(
                    selects[branch_ordinal - 1],
                    aliases,
                    read_datasets,
                    columns_by_dataset,
                    cte_column_sources,
                ):
                    for out_col in output_columns:
                        influence_facts.append(
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
                                "branch_ordinal": branch_ordinal,
                                "influence_type": source["influence_type"],
                                "expression_sql": source.get("expression_sql", ""),
                                "source_system": "sqlglot",
                                "source_type": "column_influence",
                            }
                        )
    return facts, influence_facts, errors, []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--dialect", default="spark")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    statements = load(project_dir / f"{args.prefix}_sql_statements.json", [])
    details = load(project_dir / "task_details.json", [])
    if args.limit:
        statements = statements[: args.limit]
    reads_by_stmt, writes_by_stmt = statement_dataset_edges(project_dir, args.prefix)
    outputs_by_task = graph_task_outputs(project_dir, args.prefix)
    columns_by_dataset = dms_column_index(project_dir)
    inferred_columns = infer_ctas_columns(project_dir, statements, args.dialect)
    for dataset, columns in inferred_columns.items():
        if dataset not in columns_by_dataset:
            columns_by_dataset[dataset] = columns
    details_by_task = {str(item.get("task_id")): item for item in details}

    all_facts = []
    all_influence_facts = []
    all_errors = []
    all_skipped = []
    for stmt in statements:
        facts, influence_facts, errors, skipped = extract_for_statement(
            project_dir,
            stmt,
            details_by_task,
            reads_by_stmt,
            writes_by_stmt,
            outputs_by_task,
            columns_by_dataset,
            args.dialect,
        )
        all_facts.extend(facts)
        all_influence_facts.extend(influence_facts)
        all_errors.extend(errors)
        all_skipped.extend(skipped)

    facts_path = project_dir / f"{args.prefix}_column_lineage.json"
    influence_path = project_dir / f"{args.prefix}_column_influence.json"
    errors_path = project_dir / f"{args.prefix}_column_lineage_errors.json"
    skipped_path = project_dir / f"{args.prefix}_column_lineage_skipped.json"
    facts_path.write_text(json.dumps(all_facts, ensure_ascii=False, indent=2))
    influence_path.write_text(json.dumps(all_influence_facts, ensure_ascii=False, indent=2))
    errors_path.write_text(json.dumps(all_errors, ensure_ascii=False, indent=2))
    skipped_path.write_text(json.dumps(all_skipped, ensure_ascii=False, indent=2))
    summary = {
        "statement_count": len(statements),
        "column_lineage_count": len(all_facts),
        "column_influence_count": len(all_influence_facts),
        "derived_column_lineage_count": sum(1 for item in all_facts if item.get("source_dataset")),
        "generated_column_count": sum(1 for item in all_facts if item.get("generation_type")),
        "error_count": len(all_errors),
        "skipped_count": len(all_skipped),
        "target_dataset_count": len({item["target_dataset"] for item in all_facts}),
        "source_dataset_count": len({item["source_dataset"] for item in all_facts if item["source_dataset"]}),
        "source_resolution_distribution": dict(Counter(item["source_resolution"] for item in all_facts)),
        "influence_type_distribution": dict(
            Counter(item["influence_type"] for item in all_influence_facts)
        ),
        "influence_source_resolution_distribution": dict(
            Counter(item["source_resolution"] for item in all_influence_facts)
        ),
        "generation_type_distribution": dict(
            Counter(item.get("generation_type", "") for item in all_facts if item.get("generation_type"))
        ),
        "target_resolution_distribution": dict(Counter(item["target_resolution"] for item in all_facts)),
        "output_resolution_distribution": dict(
            Counter(item.get("output_resolution", "") for item in all_facts if item.get("output_resolution"))
        ),
        "skipped_reason_distribution": dict(Counter(item["reason"] for item in all_skipped)),
        "inferred_ctas_schema_count": len(inferred_columns),
        "facts_path": str(facts_path),
        "influence_path": str(influence_path),
        "errors_path": str(errors_path),
        "skipped_path": str(skipped_path),
    }
    (project_dir / f"{args.prefix}_column_lineage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
