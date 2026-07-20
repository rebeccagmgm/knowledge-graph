# Big Data Code KG Probe

This directory contains the local prototype pipeline for building a knowledge graph from Horae scheduling metadata, runtime/page SQL, SzConnector metadata, and indicator registry facts.

## Current Status

The `trial_project` end-to-end prototype is complete and currently contains:

- 56,619 nodes and 104,828 relationships.
- 2,154 schedule tasks, 3,197 datasets, 44,904 columns, and 3,335 SQL statements.
- 319 metrics, 319 code-first definitions, and 319 registered-definition comparisons.
- A registration-driven incremental scanner with a default 48-hour interval.

The project is now at the reusable prototype stage. Multi-project validation, task-level graph patching, data-service modeling, and the formal query API remain future work.

See `TECHNICAL_SOLUTION.md` for the complete Chinese technical solution and current roadmap.

## Current Scope

- Traverse Horae upstream task lineage from one or more result task IDs.
- Collect task detail and dependency metadata.
- Collect task page SQL/config for non-hive tasks.
- Collect runtime logs for `hiveTask` and `hiveTask-2.0`.
- Parse SQL with `sqlglot`, with regex fallback for noisy runtime statements.
- Normalize common runtime-log noise before SQL parsing, including Spark/Hive config lines, execution progress lines, and task-engine messages.
- Merge SQL facts by strategy:
  - `hiveTask` / `hiveTask-2.0`: runtime log SQL.
  - Other task types: task page SQL/config.
- Collect SzConnector DMS table metadata and indicator registry metadata.
- Build graph JSONL facts and Neo4j Cypher import script.
- Extract conservative column lineage and materialize table-level dependency edges.
- Expand `*` / `alias.*` projections by DMS table schema where the source table is unambiguous.
- Resolve CTE-projected columns conservatively when their source columns can be traced through table aliases, a single read table, or schema uniqueness.
- Decorate graph facts with provenance, confidence, build metadata, and fact type.
- Audit graph facts for required properties, missing endpoints, isolated nodes, confidence distribution, and metric/table coverage.
- Export Neo4j schema constraints and indexes for common lookup fields.
- Run offline graph query validations before Neo4j import.
- Build optional LLM evidence bundles, generate code-first metric definitions, compare them with registered definitions, and merge the results into an enhanced graph.

Git repository/design-time code collection is intentionally deferred.

## Main Pipeline

Project-level multi-root task pipeline:

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --import-neo4j
```

For a task file:

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --task-file /path/to/task_ids.txt \
  --import-neo4j
```

Preview the exact commands without calling internal services:

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --dry-run
```

Build the optional LLM definition layer in mock mode:

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --build-llm
```

Call an OpenAI-compatible LLM provider after setting an API key:

```bash
export OPENAI_API_KEY="..."
export LLM_MODEL="gpt-4.1-mini"
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --build-llm \
  --llm-provider openai-compatible
```

`LLM_BASE_URL` can be set for a compatible internal gateway. If `--import-neo4j` is combined with `--build-llm`, the enhanced graph prefix is imported.

Single root task legacy pipeline:

```bash
PYTHONPATH=/Applications/personal-work/kg-local-pydeps \
python3 /Applications/personal-work/kg_probe/run_pipeline.py 238758
```

The current trial project was built from merged lineage for these 20 root tasks and lives at:

```text
/Applications/personal-work/kg-code-snapshots/projects/trial_project
```

## Key Artifacts

- `lineage.json`: merged upstream task lineage.
- `task_details.json`: Horae task details.
- `code_artifacts_page.json`: task page SQL/config artifacts.
- `log_artifacts_full.json`: runtime log artifacts for hive tasks.
- `strategy_sql_statements.json`: selected SQL statements after strategy merge.
- `strategy_dataset_edges.json`: SQL READ/WRITE table facts.
- `strategy_column_lineage.json`: conservative column lineage facts.
- `strategy_graph_nodes.jsonl`: graph nodes.
- `strategy_graph_edges.jsonl`: graph edges.
- `strategy_neo4j_import.cypher`: Neo4j import script.
- `strategy_neo4j_schema.cypher`: Neo4j constraints and indexes.
- `strategy_query_templates.cypher`: reusable Cypher query templates.
- `strategy_quality_report.json`: collection, parse, graph, and validation report.
- `strategy_graph_query_validation.json`: offline query validation samples.
- `strategy_fact_audit.json`: fact completeness, provenance, and connectivity audit.
- `llm/evidence_bundles.jsonl`: per-metric SQL/table/column/registry evidence sent to the LLM.
- `llm/code_definition_requests.jsonl`: rendered LLM requests with template id/version/hash.
- `llm/code_definitions.jsonl`: generated code-first metric definitions.
- `llm/definition_comparisons.jsonl`: comparison results against registered definitions.
- `strategy_llm_graph_nodes.jsonl`: base graph plus LLM definition facts.
- `strategy_llm_graph_edges.jsonl`: base graph plus LLM evidence, prompt, model, and comparison relations.

See `GRAPH_MODEL.md` for the fact model, confidence rules, and graph schema.

See `INCREMENTAL_UPDATE.md` for registration-driven 48-hour change detection and rebuild behavior.

Documentation index:

- `TECHNICAL_SOLUTION.md`: overall architecture, current progress, incremental updates, and query-layer plan.
- `PROJECT_PIPELINE.md`: full project build sequence and resume behavior.
- `GRAPH_MODEL.md`: node, relationship, confidence, and audit definitions.
- `INCREMENTAL_UPDATE.md`: project registration, scanning, quality gates, and rebuild behavior.
- `QUERY_LAYER_DESIGN.md`: standard response protocol and the 12 query primitives.
- `QUERY_LAYER_USAGE.md`: CLI and Python examples for the implemented query service.

## Graph Model

Main node labels:

- `Project`
- `ScheduleTask`
- `RuntimeLog`
- `Dataset`
- `Column`
- `Metric`
- `MetricDefinition`
- `Owner`
- `SqlStatement`
- `DataLayer`
- `EvidenceBundle`
- `PromptTemplate`
- `PromptRun`
- `ModelVersion`
- `CodeDefinition`
- `DefinitionComparison`

Main edge types:

- Scheduling and ownership: `HAS_ENTRY_TASK`, `DEPENDS_ON`, `OWNS`
- Task/data relations: `PRODUCES`, `CONSUMES`, `HAS_RUNTIME_LOG`
- SQL/data relations: `EMITS_SQL`, `READS`, `WRITES`
- Materialized data lineage: `DATASET_DEPENDS_ON`
- Schema and metric relations: `HAS_COLUMN`, `STORED_IN`, `COMPUTED_BY`, `HAS_DEFINITION`
- Column lineage: `DERIVED_FROM`
- Layering: `BELONGS_TO_LAYER`
- LLM definition layer: `HAS_EVIDENCE_BUNDLE`, `EVIDENCES_SQL`, `EVIDENCES_DATASET`, `EVIDENCES_COLUMN`, `HAS_CODE_DEFINITION`, `GENERATED_BY`, `USED_TEMPLATE`, `USED_EVIDENCE`, `USED_MODEL`, `HAS_COMPARISON`, `COMPARES_CODE_DEFINITION`, `COMPARES_REGISTERED_DEFINITION`

Direction convention:

- `target Dataset -[:DATASET_DEPENDS_ON]-> source Dataset`
- `target Column -[:DERIVED_FROM]-> source Column`
- `downstream ScheduleTask -[:DEPENDS_ON]-> upstream ScheduleTask`

## Validation

Run offline validation:

```bash
python3 /Applications/personal-work/kg_probe/validate_graph_queries.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy
```

Run query template export:

```bash
python3 /Applications/personal-work/kg_probe/export_query_templates.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy
```

Build only the LLM layer for an already-built graph:

```bash
python3 /Applications/personal-work/kg_probe/build_llm_evidence.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy
python3 /Applications/personal-work/kg_probe/generate_llm_requests.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project
python3 /Applications/personal-work/kg_probe/generate_code_definitions.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --provider mock
python3 /Applications/personal-work/kg_probe/compare_definitions.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --provider mock
python3 /Applications/personal-work/kg_probe/merge_llm_facts.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy \
  --output-prefix strategy_llm
```

## Neo4j Import

Neo4j was installed with Homebrew for local validation:

```bash
/opt/homebrew/bin/brew install neo4j
/opt/homebrew/opt/neo4j/bin/neo4j console
```

The local admin password for this prototype is stored at:

```text
/Applications/personal-work/kg-code-snapshots/neo4j_password.txt
```

Batch import and validate:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kg-pycache \
python3 /Applications/personal-work/kg_probe/import_and_validate_neo4j.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy \
  --batch-size 2000
```

Re-run validation without re-importing:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kg-pycache \
python3 /Applications/personal-work/kg_probe/import_and_validate_neo4j.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy \
  --skip-import
```

## Known Limits

- Column lineage is intentionally conservative. Unqualified columns are resolved only when table aliases, single-read-dataset context, or DMS schema uniqueness make the source unambiguous.
- `select *` expansion depends on DMS exact table metadata. Tables without exact DMS columns cannot be expanded safely.
- CTE lineage currently covers conservative projected-column propagation. Multi-statement temporary table chains and dynamic SQL variables still need a stronger intermediate representation.
- Some SQL parse errors are expected from runtime logs that contain engine noise or DDL fragments. Regex fallback preserves table-level facts where possible.
- Indicator registry is useful but not authoritative. Code and table metadata should win when definitions conflict.
- LLM definition generation is optional. Mock mode validates the pipeline but does not produce authoritative metric logic.
- Data services are not modeled yet.
- Neo4j has been installed and validated locally. The Homebrew daemon mode exited in this environment, while `neo4j console` works and keeps the service alive for validation.

## Pipeline Pitfalls Avoided

- Do not build graph in parallel with SQL merge or column-lineage extraction; graph building must read stable upstream artifacts.
- Build an initial graph before column lineage so task-level `PRODUCES` facts can identify target datasets.
- Run SzConnector metadata collection once after the initial graph and again after column lineage, because column parsing can reveal additional datasets.
- Use `--flush-every` for SzConnector metadata collection; writing the full DMS JSON after every table becomes very slow.
- Keep Neo4j import optional. If `--import-neo4j` is not used, `strategy_quality_report.json` marks stale Neo4j validation when database counts do not match current JSONL graph counts.
- Use `hiveTask,hiveTask-2.0` runtime logs only for hive tasks; other task types use task-page SQL/config.
- `sparkIndex` writes are read from task-page `prepare.sqls`; logs are not required for that class.
