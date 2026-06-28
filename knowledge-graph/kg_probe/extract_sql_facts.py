#!/usr/bin/env python3
"""Extract table-level SQL facts from collected runtime logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path


LOCAL_DEPS = Path(os.environ.get("KG_LOCAL_PYDEPS", "vendor"))
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

SQL_START_RE = re.compile(
    r"\b(?:insert\s+(?:overwrite|into)|create\s+(?:temporary\s+)?(?:table|view))\b",
    re.IGNORECASE,
)
LOG_PREFIX_RE = re.compile(r"^\[[^\]]+\]-\[[A-Z]+\]\s*")
EXEC_MARKER_RE = re.compile(r"^\[=>执行SQL\]\s*", re.IGNORECASE)
NOISE_LINE_RE = re.compile(
    r"^(?:connecting to|connected to|finished |job information|number of |"
    r"hive session id|time taken|ok$|slf4j|warning:|spark context|"
    r"starting job|ended job|tracking url|kill command|executequery\\b|"
    r"executing with spark engine|kerberos tgt|stage-\d+|map\s+\d+%|"
    r"reduce\s+\d+%|spark\.sql\.|hive\.)",
    re.IGNORECASE,
)
SQL_LINE_RE = re.compile(
    r"\b(?:select|with|insert|create|drop|truncate|alter|delete|from|join|where|group\s+by|order\s+by)\b",
    re.IGNORECASE,
)
READ_TABLE_RE = re.compile(r"\b(?:from|join)\s+([a-z_][\w]*\.[a-z_][\w]*)", re.IGNORECASE)
WRITE_TABLE_RE = re.compile(
    r"\b(?:insert\s+(?:overwrite|into)\s+(?:table\s+)?|"
    r"create\s+(?:temporary\s+|external\s+)?(?:table|view)\s+(?:if\s+not\s+exists\s+)?|"
    r"drop\s+table\s+(?:if\s+exists\s+)?|"
    r"truncate\s+table\s+|"
    r"alter\s+table\s+)"
    r"([a-z_][\w]*\.[a-z_][\w]*)",
    re.IGNORECASE,
)


def normalize_table_name(table: exp.Table) -> str:
    db = table.args.get("db")
    catalog = table.args.get("catalog")
    parts = []
    if catalog:
        parts.append(catalog.name.lower())
    if db:
        parts.append(db.name.lower())
    parts.append(table.name.lower())
    return ".".join(part for part in parts if part)


def layer_of(dataset: str) -> str:
    low = dataset.lower()
    if low.startswith("odata"):
        return "odata"
    if low.startswith("pdata"):
        return "pdata"
    if low.startswith("dm_index_n."):
        return "dm_index_n"
    if low.startswith("dm_") or low.startswith("dm."):
        return "dm"
    return "other"


def is_qualified_dataset(name: str) -> bool:
    low = name.lower()
    if "." not in low:
        return False
    if "://" in low or ":" in low:
        return False
    if low.startswith(("tmp.", "temp.", "default.__")):
        return False
    return bool(re.match(r"^[a-z_][\w]*\.[a-z_][\w]*$", low))


def statement_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def clean_log_text(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines = []
    for line in text.splitlines():
        line = LOG_PREFIX_RE.sub("", line).strip()
        line = EXEC_MARKER_RE.sub("", line).strip()
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def normalize_sql_for_parse(sql: str) -> str:
    sql = clean_log_text(sql)
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


def extract_candidate_sql(text: str) -> list[str]:
    text = clean_log_text(text)
    candidates = []
    collecting: list[str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if collecting is None:
            match = SQL_START_RE.search(line)
            if not match:
                continue
            collecting = [line[match.start() :]]
        else:
            if SQL_START_RE.search(line) and NOISE_LINE_RE.search(line):
                sql = "\n".join(collecting).strip()
                if len(sql) >= 40:
                    candidates.append(sql)
                collecting = None
                continue
            if NOISE_LINE_RE.search(line):
                sql = "\n".join(collecting).strip()
                if len(sql) >= 40:
                    candidates.append(sql)
                collecting = None
                continue
            collecting.append(line)

        if collecting and ";" in collecting[-1]:
            sql = "\n".join(collecting).strip()
            if len(sql) >= 40:
                candidates.append(sql)
            collecting = None

    if collecting:
        sql = "\n".join(collecting).strip()
        if len(sql) >= 40:
            candidates.append(sql)

    unique = {}
    for sql in candidates:
        unique.setdefault(statement_hash(sql), sql)
    return list(unique.values())


def extract_page_sql(text: str) -> list[str]:
    text = clean_log_text(text).strip()
    if not text:
        return []
    parts = [part.strip() for part in text.split(";") if part.strip()]
    if not parts:
        parts = [text]
    candidates = []
    for part in parts:
        low = part.lower().lstrip()
        if len(part) < 20:
            continue
        if not re.match(
            r"^(?:select\b|with\b|insert\b|create\b|drop\b|truncate\b|alter\b|delete\b)",
            low,
        ):
            continue
        candidates.append(part + (";" if not part.endswith(";") else ""))
    unique = {}
    for sql in candidates:
        unique.setdefault(statement_hash(sql), sql)
    return list(unique.values())


def write_statement_file(output_dir: Path, statement_id: str, sql: str) -> str:
    sql_dir = output_dir / "sql"
    sql_dir.mkdir(parents=True, exist_ok=True)
    path = sql_dir / f"{statement_id}.sql"
    path.write_text(sql)
    return str(path)


def regex_table_fallback(sql: str) -> tuple[set[str], set[str]]:
    reads = {name.lower() for name in READ_TABLE_RE.findall(sql) if is_qualified_dataset(name)}
    writes = {name.lower() for name in WRITE_TABLE_RE.findall(sql) if is_qualified_dataset(name)}
    return reads - writes, writes


def parse_statement(sql: str, dialect: str) -> tuple[set[str], set[str], str | None, str]:
    sql = normalize_sql_for_parse(sql)
    fallback_reads, fallback_writes = regex_table_fallback(sql)
    try:
        parsed = sqlglot.parse_one(sql, read=dialect, error_level="ignore")
    except Exception as exc:  # noqa: BLE001
        return fallback_reads, fallback_writes, str(exc), "regex"

    if parsed is None:
        return fallback_reads, fallback_writes, "empty parse result", "regex"

    write_tables: set[str] = set()
    if isinstance(parsed, exp.Insert):
        target = parsed.this
        if isinstance(target, exp.Table):
            name = normalize_table_name(target)
            if is_qualified_dataset(name):
                write_tables.add(name)
        elif target is not None:
            table = target.find(exp.Table)
            if table:
                name = normalize_table_name(table)
                if is_qualified_dataset(name):
                    write_tables.add(name)
    elif isinstance(parsed, (exp.Create, exp.Drop)):
        target = parsed.this
        if isinstance(target, exp.Table):
            name = normalize_table_name(target)
            if is_qualified_dataset(name):
                write_tables.add(name)

    all_tables = {
        name
        for name in (normalize_table_name(table) for table in parsed.find_all(exp.Table))
        if is_qualified_dataset(name)
    }
    cte_names = {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}
    read_tables = {
        name for name in all_tables - write_tables if name.split(".")[-1] not in cte_names
    }
    if not read_tables and not write_tables and (fallback_reads or fallback_writes):
        return fallback_reads, fallback_writes, "sqlglot produced no table facts", "regex"
    return read_tables, write_tables, None, "sqlglot"


def extract_project(project_dir: Path, dialect: str, log_artifacts_file: str) -> dict:
    log_artifacts_path = project_dir / log_artifacts_file
    if not log_artifacts_path.exists():
        raise SystemExit(f"Missing {log_artifacts_path}")

    log_artifacts = json.loads(log_artifacts_path.read_text())
    statements = []
    datasets = {}
    edges = []
    errors = []

    for artifact in log_artifacts:
        log_path = Path(artifact["path"])
        if not log_path.exists():
            errors.append({"task_id": artifact["task_id"], "error": f"missing log {log_path}"})
            continue
        text = log_path.read_text(errors="ignore")
        is_page_artifact = str(artifact.get("source_type", "")).startswith("task_page")
        candidates = extract_page_sql(text) if is_page_artifact else extract_candidate_sql(text)
        for index, sql in enumerate(candidates, start=1):
            sql = normalize_sql_for_parse(sql)
            if len(sql) < 20:
                continue
            sid = f"{artifact['task_id']}_{statement_hash(sql)}"
            stmt_path = write_statement_file(project_dir, sid, sql)
            reads, writes, error, extraction_method = parse_statement(sql, dialect)
            if error and not reads and not writes:
                errors.append(
                    {
                        "task_id": artifact["task_id"],
                        "statement_id": sid,
                        "error": error,
                        "status": "skipped_no_table_facts",
                    }
                )
                continue
            if error:
                errors.append(
                    {
                        "task_id": artifact["task_id"],
                        "statement_id": sid,
                        "error": error,
                        "status": "regex_table_facts_extracted",
                    }
                )

            statements.append(
                {
                    "statement_id": sid,
                    "task_id": artifact["task_id"],
                    "source_log_path": str(log_path),
                    "statement_path": stmt_path,
                    "statement_index": index,
                    "sha256_16": sid.split("_", 1)[1],
                    "bytes": len(sql.encode("utf-8")),
                    "parse_ok": error is None,
                    "extraction_method": extraction_method,
                    "read_dataset_count": len(reads),
                    "write_dataset_count": len(writes),
                    "source_system": "horae",
                    "source_type": "runtime_log_sql",
                }
            )
            for dataset in sorted(reads | writes):
                datasets[dataset] = {"dataset": dataset, "layer": layer_of(dataset)}
            for dataset in sorted(reads):
                edges.append(
                    {
                        "from": dataset,
                        "to": sid,
                        "relation": "READ_BY",
                        "task_id": artifact["task_id"],
                        "source_type": "sqlglot_table_lineage",
                    }
                )
            for dataset in sorted(writes):
                edges.append(
                    {
                        "from": sid,
                        "to": dataset,
                        "relation": "WRITES",
                        "task_id": artifact["task_id"],
                        "source_type": "sqlglot_table_lineage",
                    }
                )

    result = {
        "statements": statements,
        "datasets": sorted(datasets.values(), key=lambda item: item["dataset"]),
        "edges": edges,
        "errors": errors,
        "summary": {
            "statement_count": len(statements),
            "parse_ok_count": sum(1 for item in statements if item["parse_ok"]),
            "dataset_count": len(datasets),
            "edge_count": len(edges),
            "dataset_layer_distribution": dict(Counter(item["layer"] for item in datasets.values())),
            "error_count": len(errors),
        },
    }
    return result


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--dialect", default="hive")
    parser.add_argument("--log-artifacts", default="log_artifacts.json")
    parser.add_argument("--prefix", default="")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    result = extract_project(project_dir, args.dialect, args.log_artifacts)
    prefix = f"{args.prefix}_" if args.prefix else ""
    write_json(project_dir / f"{prefix}sql_statements.json", result["statements"])
    write_json(project_dir / f"{prefix}datasets.json", result["datasets"])
    write_json(project_dir / f"{prefix}dataset_edges.json", result["edges"])
    write_json(project_dir / f"{prefix}sql_parse_errors.json", result["errors"])
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
