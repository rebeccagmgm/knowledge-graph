# Project Pipeline

`run_project_pipeline.py` is the reusable entrypoint for a project with multiple result task IDs.

## Basic Usage

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --tasks 236334,212769,207174 \
  --import-neo4j
```

Task file format can be comma-separated or one ID per line:

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file /Applications/personal-work/my_project_tasks.txt \
  --import-neo4j
```

Dry run:

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --tasks 236334,212769,207174 \
  --dry-run
```

Optional LLM metric-definition layer:

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file /Applications/personal-work/my_project_tasks.txt \
  --build-llm
```

Real LLM calls use an OpenAI-compatible endpoint:

```bash
export OPENAI_API_KEY="..."
export LLM_MODEL="gpt-4.1-mini"
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file /Applications/personal-work/my_project_tasks.txt \
  --build-llm \
  --llm-provider openai-compatible
```

Set `LLM_BASE_URL` when using an internal OpenAI-compatible gateway. The default provider is `mock`, which validates the evidence/request/graph path without calling a model.

## Output Layout

For `--project-id my_project`:

```text
/Applications/personal-work/kg-code-snapshots/lineage_batch/my_project
/Applications/personal-work/kg-code-snapshots/projects/my_project
```

Important final artifacts:

- `lineage.json`
- `task_details.json`
- `strategy_sql_statements.json`
- `strategy_dataset_edges.json`
- `strategy_column_lineage.json`
- `strategy_graph_nodes.jsonl`
- `strategy_graph_edges.jsonl`
- `strategy_fact_audit.json`
- `strategy_neo4j_schema.cypher`
- `strategy_neo4j_import.cypher`
- `strategy_query_templates.cypher`
- `strategy_graph_query_validation.json`
- `strategy_neo4j_validation.json` if `--import-neo4j` is used
- `strategy_quality_report.json`
- `llm/evidence_bundles.jsonl` if `--build-llm` is used
- `llm/code_definition_requests.jsonl` if `--build-llm` is used
- `llm/code_definitions.jsonl` if `--build-llm` is used
- `llm/definition_comparisons.jsonl` if `--build-llm` is used
- `strategy_llm_graph_nodes.jsonl` if `--build-llm` is used
- `strategy_llm_graph_edges.jsonl` if `--build-llm` is used
- `project_pipeline_manifest.json`

## Step Order

1. Collect upstream lineage for each result task.
2. Merge all lineage snapshots into one project-level lineage graph.
3. Collect Horae task details.
4. Collect task-page SQL/config.
5. Collect runtime logs only for `hiveTask,hiveTask-2.0`.
6. Parse hive log SQL.
7. Parse page SQL.
8. Merge SQL facts by task-type strategy.
9. Build initial graph.
10. Collect SzConnector metadata from graph datasets.
11. Extract column lineage.
12. Rebuild graph.
13. Re-collect SzConnector metadata for newly surfaced graph datasets.
14. Repeat column-lineage pass if configured.
15. Build final graph.
16. Audit facts.
17. Export Neo4j schema, import Cypher, and query templates.
18. Run offline graph query validation.
19. Optionally build LLM evidence, definitions, comparisons, and an enhanced graph.
20. Optionally audit, export, and validate the LLM-enhanced graph.
21. Optionally import and validate Neo4j. With `--build-llm`, the imported prefix is `strategy_llm`.
22. Generate final quality report.

## Resume Behavior

The pipeline is designed for reruns:

- Lineage batch skips roots already present in the lineage batch directory.
- Task details reuse `task_details.json` unless `--force-details` is set.
- Page code reuse `code_artifacts_page.json` unless `--force-page-code` is set.
- Hive logs reuse `log_artifacts_full.json` unless `--force-logs` is set.
- SzConnector metadata skips datasets already present in `sz_metadata/dataset_dms.json` and `indicator_registry.json`.

## Force Flags

- `--force-details`: recollect task detail pages.
- `--force-page-code`: recollect task page SQL/config artifacts.
- `--force-logs`: recollect hive runtime logs.

Avoid force flags for large projects unless necessary.

## LLM Flags

- `--build-llm`: build the optional metric definition layer.
- `--llm-provider mock`: validate locally without model calls.
- `--llm-provider openai-compatible`: call `/chat/completions` using `OPENAI_API_KEY` or `LLM_API_KEY`.
- `--llm-model`: override `LLM_MODEL`.
- `--llm-output-prefix`: override the enhanced graph prefix. Default is `strategy_llm`.
- `--llm-max-sql-chars`: cap SQL text per statement in evidence bundles.
- `--llm-sleep`: throttle real model requests.

## Neo4j

Use `--import-neo4j` only when local Neo4j is running and you want the database updated.

Without `--import-neo4j`, the final quality report still runs. It will set:

```json
"has_stale_neo4j_validation": true
```

when the previous Neo4j validation no longer matches the current graph JSONL counts.

## Known Project-Level Assumptions

- Hive runtime SQL is authoritative for `hiveTask` and `hiveTask-2.0`.
- Task-page SQL/config is authoritative for non-hive tasks.
- `sparkIndex` target/write logic is read from task-page `prepare.sqls`.
- Git repository collection is intentionally deferred.
- Data-service nodes are intentionally deferred.

## Incremental Update Integration

`incremental_update.py` sits in front of this pipeline:

1. Read project result and supplemental task IDs from the registry.
2. Refresh upstream lineage, task details, page code, and Hive runtime SQL.
3. Stop if the refresh quality gate reports missing or failed artifacts.
4. Compare task metadata, dependency edges, raw code hashes, and normalized SQL semantic hashes.
5. Return immediately when there is no semantic change.
6. Compute affected downstream tasks and metrics when a change exists.
7. Invoke this full project pipeline to rebuild and validate the project.

The first incremental version deliberately rebuilds the project instead of patching Neo4j task by task. See `INCREMENTAL_UPDATE.md` for commands and operational files.
