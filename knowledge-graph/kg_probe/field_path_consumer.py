"""Consume existing KG facts to build target-field upstream paths.

This module intentionally sits outside the KG builders and graph model.  It
reads the existing JSON/JSONL artifacts and adds a target-directed view over
them.  Unresolved KG column facts may be narrowed with explicit SQL projection
aliases, but no KG node or edge is changed.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - the repository declares sqlglot
    sqlglot = None
    exp = None


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9_.]+", "", str(value or "").strip().lower())


def field_norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(value or "").strip().lower())


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


@dataclass(frozen=True)
class SourceRef:
    dataset: str
    column: str
    status: str
    evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class FieldPathConsumer:
    """Build field paths from one existing KG project artifact directory."""

    def __init__(self, project_dir: Path, prefix: str = "strategy") -> None:
        self.project_dir = Path(project_dir)
        self.prefix = prefix
        self.facts = load_json(self.project_dir / f"{prefix}_column_lineage.json", [])
        self.nodes = load_jsonl(self.project_dir / f"{prefix}_graph_nodes.jsonl")
        self.edges = load_jsonl(self.project_dir / f"{prefix}_graph_edges.jsonl")
        self.statements = load_json(self.project_dir / f"{prefix}_sql_statements.json", [])

        self.tasks_by_dataset: dict[str, list[str]] = defaultdict(list)
        self.datasets_by_task: dict[str, list[str]] = defaultdict(list)
        self.tasks_by_dataset_alias: dict[str, list[str]] = defaultdict(list)
        self.upstream_by_task: dict[str, set[str]] = defaultdict(set)
        self.facts_by_task_dataset_field: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.facts_by_task_field: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.sql_by_task: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.projection_sources: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.relation_aliases: dict[tuple[str, str], str] = {}
        self._index()

    def _index(self) -> None:
        sql_to_task: dict[str, str] = {}
        for edge in self.edges:
            edge_type = edge.get("type")
            if edge_type == "EMITS_SQL":
                task_id = str(edge.get("from", "")).removeprefix("task:")
                statement_id = str(edge.get("to", "")).removeprefix("sql:")
                if task_id and statement_id:
                    sql_to_task[statement_id] = task_id
            elif edge_type == "PRODUCES":
                task_id = str(edge.get("from", "")).removeprefix("task:")
                dataset_id = str(edge.get("to", ""))
                if task_id and dataset_id.startswith("dataset:"):
                    dataset = norm(dataset_id.removeprefix("dataset:"))
                    if task_id not in self.tasks_by_dataset[dataset]:
                        self.tasks_by_dataset[dataset].append(task_id)
                    if dataset not in self.datasets_by_task[task_id]:
                        self.datasets_by_task[task_id].append(dataset)
                    alias = self._dataset_alias(dataset)
                    if task_id not in self.tasks_by_dataset_alias[alias]:
                        self.tasks_by_dataset_alias[alias].append(task_id)
            elif edge_type == "DEPENDS_ON":
                downstream = str(edge.get("from", "")).removeprefix("task:")
                upstream = str(edge.get("to", "")).removeprefix("task:")
                if downstream and upstream:
                    self.upstream_by_task[downstream].add(upstream)

        # A task can expose its final target only through its parsed SQL:
        # task -EMITS_SQL-> statement -WRITES-> dataset.  Reuse that existing
        # KG evidence without changing the graph or inventing a scheduler edge.
        for edge in self.edges:
            if edge.get("type") != "WRITES":
                continue
            statement_id = str(edge.get("from", "")).removeprefix("sql:")
            dataset_id = str(edge.get("to", ""))
            task_id = sql_to_task.get(statement_id)
            if task_id and dataset_id.startswith("dataset:"):
                dataset = norm(dataset_id.removeprefix("dataset:"))
                if task_id not in self.tasks_by_dataset[dataset]:
                    self.tasks_by_dataset[dataset].append(task_id)
                if dataset not in self.datasets_by_task[task_id]:
                    self.datasets_by_task[task_id].append(dataset)
                alias = self._dataset_alias(dataset)
                if task_id not in self.tasks_by_dataset_alias[alias]:
                    self.tasks_by_dataset_alias[alias].append(task_id)
        for tasks in self.tasks_by_dataset.values():
            tasks.sort()
        for tasks in self.datasets_by_task.values():
            tasks.sort()
        for tasks in self.tasks_by_dataset_alias.values():
            tasks.sort()

        for fact in self.facts:
            task_id = str(fact.get("task_id", ""))
            dataset = norm(fact.get("target_dataset"))
            column = field_norm(fact.get("target_column"))
            if task_id and dataset and column:
                self.facts_by_task_dataset_field[(task_id, dataset, column)].append(fact)
                self.facts_by_task_field[(task_id, column)].append(fact)

        for statement in self.statements:
            task_id = str(statement.get("task_id", ""))
            statement_id = str(statement.get("statement_id", ""))
            path = statement.get("statement_path")
            if not task_id or not path or not Path(path).exists():
                continue
            sql = Path(path).read_text(encoding="utf-8", errors="replace")
            self.sql_by_task[task_id].append((statement_id, sql))
            self._index_sql(task_id, statement_id, sql)

    @staticmethod
    def _dataset_alias(dataset: str) -> str:
        """Normalize logical SQL table names to task output identities.

        SQL lineage may expose ``..._tit165`` as ``...`` while Horae task
        metadata keeps the physical/task-specific suffix.  The alias is only
        a candidate lookup; branch reachability below decides whether it is
        safe to use.
        """
        return re.sub(r"_tit\d+$", "", norm(dataset))

    def _upstream_closure(self, task_id: str) -> set[str]:
        closure = {str(task_id)}
        pending = [str(task_id)]
        while pending:
            current = pending.pop()
            for upstream in self.upstream_by_task.get(current, set()):
                if upstream not in closure:
                    closure.add(upstream)
                    pending.append(upstream)
        return closure

    def _producer_tasks(self, dataset: str, allowed_tasks: set[str] | None = None) -> list[str]:
        dataset = norm(dataset)
        candidates = list(self.tasks_by_dataset.get(dataset, []))
        if not candidates:
            candidates = list(self.tasks_by_dataset_alias.get(self._dataset_alias(dataset), []))
        if allowed_tasks is not None:
            candidates = [task_id for task_id in candidates if task_id in allowed_tasks]
        return sorted(set(candidates))

    def _fact_target_datasets(self, task_id: str, dataset: str, column: str) -> list[str]:
        dataset = norm(dataset)
        column = field_norm(column)
        candidates = [dataset]
        for output_dataset in self.datasets_by_task.get(str(task_id), []):
            if self._dataset_alias(output_dataset) == self._dataset_alias(dataset):
                candidates.append(output_dataset)
        return list(dict.fromkeys(candidates))

    def _index_sql(self, task_id: str, statement_id: str, sql: str) -> None:
        qualified = re.compile(
            r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\s+as\s+([a-z_][a-z0-9_]*)",
            re.IGNORECASE,
        )
        unqualified = re.compile(
            r"(?<![.\w])([a-z_][a-z0-9_]*)\s+as\s+([a-z_][a-z0-9_]*)",
            re.IGNORECASE,
        )
        for match in qualified.finditer(sql):
            alias, source_column, target_column = match.groups()
            self.projection_sources[(task_id, field_norm(target_column))].append(
                {
                    "source_alias": field_norm(alias),
                    "source_column": field_norm(source_column),
                    "statement_id": statement_id,
                    "evidence_kind": "SQL_QUALIFIED_PROJECTION",
                }
            )
        for match in unqualified.finditer(sql):
            source_column, target_column = match.groups()
            if source_column.lower() in {"from", "join", "where", "case", "when", "then", "else"}:
                continue
            self.projection_sources[(task_id, field_norm(target_column))].append(
                {
                    "source_alias": "",
                    "source_column": field_norm(source_column),
                    "statement_id": statement_id,
                    "evidence_kind": "SQL_UNQUALIFIED_PROJECTION",
                }
            )

        nested_relation = re.compile(
            r"\b(?:from|join)\s*\(\s*select[\s\S]*?\bfrom\s+([a-z_][a-z0-9_.]*)[\s\S]*?\)\s*(?:as\s+)?([a-z_][a-z0-9_]*)",
            re.IGNORECASE,
        )
        simple_relation = re.compile(
            r"\b(?:from|join)\s+([a-z_][a-z0-9_.]*)\s+(?:as\s+)?([a-z_][a-z0-9_]*)",
            re.IGNORECASE,
        )
        for pattern in (nested_relation, simple_relation):
            for match in pattern.finditer(sql):
                dataset, alias = match.groups()
                self.relation_aliases[(task_id, field_norm(alias))] = norm(dataset)

    def _fact_status(self, fact: dict[str, Any]) -> str:
        if fact.get("source_dataset") and fact.get("source_column"):
            return "CONFIRMED"
        return "CANDIDATE"

    def _projection_refs(self, task_id: str, column: str) -> list[SourceRef]:
        refs: list[SourceRef] = []
        for item in self.projection_sources.get((task_id, field_norm(column)), []):
            dataset = self.relation_aliases.get((task_id, item["source_alias"]), "")
            refs.append(
                SourceRef(
                    dataset=dataset,
                    column=item["source_column"],
                    status="CONFIRMED" if dataset else "CANDIDATE",
                    evidence=(
                        {
                            "kind": item["evidence_kind"],
                            "statement_id": item["statement_id"],
                            "source_alias": item["source_alias"],
                        },
                    ),
                )
            )
        return refs

    def _resolve_any_task_field(
        self,
        task_id: str,
        column: str,
        seen: set[tuple[str, str, str]],
    ) -> list[SourceRef]:
        refs: list[SourceRef] = []
        datasets = sorted({
            dataset
            for fact_task, dataset, fact_column in self.facts_by_task_dataset_field
            if fact_task == task_id and fact_column == field_norm(column)
        })
        for dataset in datasets:
            refs.extend(self._resolve_task_field(task_id, dataset, column, seen))
        return self._dedupe_refs(refs)

    def _resolve_task_field(
        self,
        task_id: str,
        dataset: str,
        column: str,
        seen: set[tuple[str, str, str]],
    ) -> list[SourceRef]:
        key = (task_id, norm(dataset), field_norm(column))
        if key in seen:
            return []
        next_seen = set(seen)
        next_seen.add(key)
        refs: list[SourceRef] = []
        facts: list[dict[str, Any]] = []
        for fact_dataset in self._fact_target_datasets(task_id, dataset, column):
            facts.extend(self.facts_by_task_dataset_field.get((task_id, fact_dataset, field_norm(column)), []))
        for fact in facts:
            if fact.get("source_dataset") and fact.get("source_column"):
                refs.append(
                    SourceRef(
                        dataset=norm(fact["source_dataset"]),
                        column=field_norm(fact["source_column"]),
                        status=self._fact_status(fact),
                        evidence=(
                            {
                                "kind": "KG_DERIVED_FROM",
                                "task_id": task_id,
                                "statement_id": fact.get("statement_id"),
                                "source_resolution": fact.get("source_resolution"),
                            },
                        ),
                    )
                )
                continue

            projection_refs = self._projection_refs(task_id, fact.get("target_column", column))
            for projection in projection_refs:
                if projection.dataset:
                    refs.append(projection)
                    continue
                for nested in self._resolve_any_task_field(task_id, projection.column, next_seen):
                    refs.append(
                        SourceRef(
                            dataset=nested.dataset,
                            column=nested.column,
                            status="CANDIDATE" if projection.status != "CONFIRMED" else nested.status,
                            evidence=projection.evidence + nested.evidence,
                        )
                    )

        return self._dedupe_refs(refs)

    @staticmethod
    def _dedupe_refs(refs: Iterable[SourceRef]) -> list[SourceRef]:
        result: dict[tuple[str, str], SourceRef] = {}
        rank = {"CONFIRMED": 2, "CANDIDATE": 1, "UNKNOWN": 0}
        for ref in refs:
            key = (norm(ref.dataset), field_norm(ref.column))
            if not key[0] or not key[1]:
                continue
            current = result.get(key)
            if current is None or rank[ref.status] > rank[current.status]:
                result[key] = ref
        return list(result.values())

    def _controls_for_tasks(self, task_ids: Iterable[str]) -> list[dict[str, Any]]:
        controls: list[dict[str, Any]] = []
        if sqlglot is None:
            return controls
        for task_id in task_ids:
            for statement_id, sql in self.sql_by_task.get(str(task_id), []):
                try:
                    trees = sqlglot.parse(sql, read="hive")
                except Exception:
                    continue
                for tree in trees:
                    for select in tree.find_all(exp.Select):
                        where = select.args.get("where")
                        if where is not None:
                            controls.append(
                                {
                                    "kind": "ROWSET_CONTROL",
                                    "control_type": "FILTER",
                                    "task_id": str(task_id),
                                    "statement_id": statement_id,
                                    "expression": where.this.sql(dialect="hive"),
                                }
                            )
                        for join in select.args.get("joins") or []:
                            on = join.args.get("on")
                            if on is not None:
                                controls.append(
                                    {
                                        "kind": "ROWSET_CONTROL",
                                        "control_type": "JOIN",
                                        "task_id": str(task_id),
                                        "statement_id": statement_id,
                                        "expression": on.sql(dialect="hive"),
                                    }
                                )
        unique = {(item["task_id"], item["statement_id"], item["control_type"], item["expression"]): item for item in controls}
        return list(unique.values())

    def _walk(
        self,
        dataset: str,
        column: str,
        path: list[dict[str, Any]],
        visited: set[tuple[str, str]],
        max_depth: int,
        task_id: str | None = None,
        allowed_tasks: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        dataset = norm(dataset)
        column = field_norm(column)
        producer_tasks = self._producer_tasks(dataset, allowed_tasks)
        if task_id is None and len(producer_tasks) > 1:
            paths: list[dict[str, Any]] = []
            for producer_task in producer_tasks:
                paths.extend(self._walk(dataset, column, path, visited, max_depth, producer_task, allowed_tasks))
            for item in paths:
                item.setdefault("warnings", []).append(
                    {
                        "code": "MULTIPLE_PRODUCERS",
                        "dataset": dataset,
                        "producer_task_ids": producer_tasks,
                    }
                )
                if item["status"] == "CONFIRMED":
                    item["status"] = "CANDIDATE"
            return paths
        task_id = task_id or (producer_tasks[0] if producer_tasks else None)
        step = {
            "kind": "VALUE_FLOW",
            "task_id": task_id,
            "dataset": dataset,
            "field": column,
        }
        next_path = path + [step]
        if not task_id:
            return [{"status": "CONFIRMED", "steps": next_path, "terminal": {"dataset": dataset, "field": column}}]
        if len(path) >= max_depth:
            return [{"status": "PARTIAL", "steps": next_path, "gap": "MAX_DEPTH"}]
        visit_key = (dataset, column)
        if visit_key in visited:
            return [{"status": "PARTIAL", "steps": next_path, "gap": "CYCLE"}]

        sources = self._resolve_task_field(task_id, dataset, column, set())
        if not sources:
            return [{"status": "PARTIAL", "steps": next_path, "gap": "FIELD_SOURCE_UNRESOLVED"}]

        results: list[dict[str, Any]] = []
        for source in sources:
            link = {
                "kind": "VALUE_FLOW",
                "from": {"dataset": dataset, "field": column, "task_id": task_id},
                "to": {"dataset": source.dataset, "field": source.column, "task_id": None},
                "status": source.status,
                "evidence": list(source.evidence),
            }
            producer_tasks = self._producer_tasks(source.dataset, allowed_tasks)
            if producer_tasks and (source.dataset, source.column) not in visited:
                for producer_task in producer_tasks:
                    descendants = self._walk(
                        source.dataset,
                        source.column,
                        next_path,
                        visited | {visit_key},
                        max_depth,
                        producer_task,
                        allowed_tasks,
                    )
                    for descendant in descendants:
                        if len(producer_tasks) > 1:
                            descendant.setdefault("warnings", []).append(
                                {
                                    "code": "MULTIPLE_PRODUCERS",
                                    "dataset": source.dataset,
                                    "producer_task_ids": producer_tasks,
                                }
                            )
                            if descendant["status"] == "CONFIRMED":
                                descendant["status"] = "CANDIDATE"
                        descendant["links"] = [
                            link
                            | {
                                "to": {**link["to"], "task_id": producer_task},
                                "evidence": list(link.get("evidence", []))
                                + [{"kind": "KG_PRODUCES", "task_id": producer_task}],
                            }
                        ] + descendant.get("links", [])
                        if source.status == "CANDIDATE" and descendant["status"] == "CONFIRMED":
                            descendant["status"] = "CANDIDATE"
                        results.append(descendant)
            else:
                results.append(
                    {
                        "status": source.status,
                        "steps": next_path + [
                            {
                                "kind": "VALUE_FLOW",
                                "task_id": None,
                                "dataset": source.dataset,
                                "field": source.column,
                            }
                        ],
                        "links": [link],
                        "terminal": {"dataset": source.dataset, "field": source.column},
                    }
                )
        return results

    @staticmethod
    def _prune_dominated(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove a shorter path when another path continues through it.

        KG task-local temporary datasets can appear as unresolved leaves while
        a second fact continues through that same temporary field to a real
        producer task.  Keeping both would defeat the minimal-path view.
        """
        rank = {"CONFIRMED": 2, "CANDIDATE": 1, "PARTIAL": 0}
        kept: list[dict[str, Any]] = []
        for candidate in paths:
            terminal = candidate.get("terminal") or {}
            terminal_key = (terminal.get("dataset"), terminal.get("field"))
            dominated = False
            for other in paths:
                if other is candidate or len(other.get("steps", [])) <= len(candidate.get("steps", [])):
                    continue
                if rank.get(other.get("status", "PARTIAL"), 0) < rank.get(candidate.get("status", "PARTIAL"), 0):
                    continue
                if isinstance(terminal_key[0], str) and terminal_key[0].startswith("temp."):
                    dominated = True
                    break
                if terminal_key in {
                    (step.get("dataset"), step.get("field"))
                    for step in other.get("steps", [])
                }:
                    dominated = True
                    break
            if not dominated:
                kept.append(candidate)
        return kept

    def build(self, target_dataset: str, fields: Iterable[str] | None = None, max_depth: int = 12) -> dict[str, Any]:
        target_dataset = norm(target_dataset)
        requested = [field_norm(item) for item in fields or [] if field_norm(item)]
        if not requested:
            requested = sorted({
                column
                for task_id, dataset, column in self.facts_by_task_dataset_field
                if dataset == target_dataset
            })
        paths: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for column in requested:
            root_producers = self._producer_tasks(target_dataset)
            if root_producers:
                field_paths = []
                for root_task in root_producers:
                    scope = self._upstream_closure(root_task)
                    if root_task not in self.upstream_by_task:
                        # Small/unit-test artifacts may contain PRODUCES edges
                        # without scheduler lineage.  Do not discard a valid
                        # producer merely because the optional scope evidence
                        # is absent.
                        scope = None
                    field_paths.extend(
                        self._walk(
                            target_dataset,
                            column,
                            [],
                            set(),
                            max_depth,
                            root_task,
                            scope,
                        )
                    )
            else:
                field_paths = self._walk(target_dataset, column, [], set(), max_depth)
            field_paths = self._prune_dominated(field_paths)
            if not field_paths:
                gaps.append({"field": column, "reason": "NO_PATH"})
                continue
            for field_path in field_paths:
                field_path["target"] = {"dataset": target_dataset, "field": column}
                field_path["rowset_control"] = self._controls_for_tasks(
                    step.get("task_id") for step in field_path.get("steps", []) if step.get("task_id")
                )
                paths.append(field_path)

        complete_fields = {item["target"]["field"] for item in paths if item["status"] in {"CONFIRMED", "CANDIDATE"}}
        overall = "COMPLETE" if len(complete_fields) == len(requested) and not gaps and all(item["status"] == "CONFIRMED" for item in paths) else "PARTIAL"
        return {
            "artifact_type": "KG_FIELD_PATH_CONSUMPTION",
            "project_dir": str(self.project_dir),
            "target": {"dataset": target_dataset, "fields": requested},
            "status": overall,
            "paths": paths,
            "gaps": gaps,
            "summary": {
                "requested_field_count": len(requested),
                "path_count": len(paths),
                "confirmed_path_count": sum(1 for item in paths if item["status"] == "CONFIRMED"),
                "candidate_path_count": sum(1 for item in paths if item["status"] == "CANDIDATE"),
                "partial_path_count": sum(1 for item in paths if item["status"] == "PARTIAL"),
                "gap_count": len(gaps),
                "kg_base_modified": False,
            },
        }


def parse_fields(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Consume existing KG facts into target-field upstream paths.")
    parser.add_argument("project_dir")
    parser.add_argument("--target-dataset", required=True)
    parser.add_argument("--fields", default=None)
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--max-depth", type=int, default=12)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    result = FieldPathConsumer(Path(args.project_dir), args.prefix).build(
        args.target_dataset,
        parse_fields(args.fields),
        args.max_depth,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
