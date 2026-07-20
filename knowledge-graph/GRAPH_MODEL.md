# Knowledge Graph Fact Model

## Layer Boundary

The prototype separates the pipeline into:

- Collection layer: raw task, log, SQL/config, DMS, and indicator registry artifacts.
- Parsing layer: normalized SQL statements plus table and column lineage facts.
- Fact layer: graph-ready entities and relationships with provenance, confidence, and evidence metadata.
- Graph layer: Neo4j schema, JSONL graph facts, import scripts, validation queries, and Cypher templates.
- LLM definition layer: optional evidence bundles, prompt/model runs, code-first metric definitions, and comparisons against registered definitions.

## Universal Fact Properties

Every graph node and edge is decorated with:

- `fact_type`: semantic category, such as `table_lineage`, `column_lineage`, `business_metric`, or `schedule_task`.
- `project_key`: project/root lineage key.
- `graph_prefix`: fact set prefix, for example `strategy`.
- `build_id`: graph build identifier.
- `built_at`: build timestamp.
- `confidence`: `high`, `medium`, `low`, or `unknown`.
- `inferred`: whether the fact was inferred rather than directly collected.

Edges additionally include:

- `evidence_from_id`
- `evidence_to_id`

Specific edge types also carry statement, task, source-resolution, or target-resolution evidence.

## Core Nodes

- `Project`: one KG project snapshot.
- `ScheduleTask`: Horae scheduling task.
- `RuntimeLog`: runtime log artifact.
- `SqlStatement`: normalized SQL statement artifact.
- `Dataset`: table/data asset.
- `Column`: dataset column.
- `Metric`: indicator registry metric.
- `MetricDefinition`: indicator registry definition text.
- `Owner`: owner/developer/designer.
- `DataLayer`: `odata`, `pdata`, `dm_index_n`, `dm`, `other`, `root_unknown`.
- `EvidenceBundle`: per-metric evidence assembled from graph facts, SQL snippets, read/write tables, target columns, and registry text.
- `PromptTemplate`: versioned prompt template. The template text is stored once with a hash.
- `PromptRun`: one model invocation or mock invocation for definition generation or definition comparison.
- `ModelVersion`: provider/model identity used by a prompt run.
- `CodeDefinition`: code-first metric definition generated from SQL and graph evidence.
- `DefinitionComparison`: comparison between code-first and registered definitions.

## Core Edges

Scheduling:

- `Project -[:HAS_ENTRY_TASK]-> ScheduleTask`
- `downstream ScheduleTask -[:DEPENDS_ON]-> upstream ScheduleTask`

Task/data:

- `ScheduleTask -[:PRODUCES]-> Dataset`
- `ScheduleTask -[:CONSUMES]-> Dataset`
- `ScheduleTask -[:HAS_RUNTIME_LOG]-> RuntimeLog`
- `ScheduleTask -[:EMITS_SQL]-> SqlStatement`

SQL/table lineage:

- `SqlStatement -[:READS]-> Dataset`
- `SqlStatement -[:WRITES]-> Dataset`
- `target Dataset -[:DATASET_DEPENDS_ON]-> source Dataset`

Column lineage:

- `Dataset -[:HAS_COLUMN]-> Column`
- `target Column -[:DERIVED_FROM]-> source Column`
- `target Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression`

Metric/business:

- `Metric -[:STORED_IN]-> Dataset`
- `Metric -[:COMPUTED_BY]-> ScheduleTask`
- `Metric -[:HAS_DEFINITION]-> MetricDefinition`
- `Metric -[:HAS_EVIDENCE_BUNDLE]-> EvidenceBundle`
- `Metric -[:HAS_CODE_DEFINITION]-> CodeDefinition`

LLM evidence and generation:

- `EvidenceBundle -[:EVIDENCES_SQL]-> SqlStatement`
- `EvidenceBundle -[:EVIDENCES_DATASET]-> Dataset`
- `EvidenceBundle -[:EVIDENCES_COLUMN]-> Column`
- `CodeDefinition -[:GENERATED_BY]-> PromptRun`
- `PromptRun -[:USED_TEMPLATE]-> PromptTemplate`
- `PromptRun -[:USED_EVIDENCE]-> EvidenceBundle`
- `PromptRun -[:USED_MODEL]-> ModelVersion`
- `CodeDefinition -[:HAS_COMPARISON]-> DefinitionComparison`
- `DefinitionComparison -[:GENERATED_BY]-> PromptRun`
- `DefinitionComparison -[:COMPARES_CODE_DEFINITION]-> CodeDefinition`
- `DefinitionComparison -[:COMPARES_REGISTERED_DEFINITION]-> MetricDefinition`

Ownership/layering:

- `Owner -[:OWNS]-> ScheduleTask | Dataset | Metric`
- `Dataset | ScheduleTask -[:BELONGS_TO_LAYER]-> DataLayer`

## Confidence Rules

High confidence:

- Direct Horae scheduling dependencies.
- Direct DMS table/column metadata.
- Direct indicator registry storage/definition facts.
- SQLGlot parsed `READS` / `WRITES`.
- Column lineage from explicit table aliases.

Medium confidence:

- Dataset dependency inferred from schedule dependency outputs.
- Column lineage from single-read-table context, schema-unique column matching, star expansion, or CTE propagation.
- Metric compute task inferred from metric storage dataset producer.
- Ownership and layer rules derived from metadata/name conventions.
- LLM output from real model calls unless later promoted by human review.

Low confidence:

- Mock LLM definitions and comparisons.
- Reserved for unresolved or weakly inferred facts. Current graph build does not emit low-confidence `DERIVED_FROM` edges.

## Generated Column Values

Some target fields are not derived from source table columns. Examples include fixed values
(`'RCC' AS data_src_cd`), blank placeholders (`'' AS remark`), partition values, and runtime
expressions (`from_unixtime(unix_timestamp()) AS data_time`).

These are modeled explicitly instead of being counted as column-lineage errors:

- Node: `GeneratedExpression`
- Edge: `Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression`
- Key properties:
  - `generation_type`: `literal`, `generated_expression`, or `system_expression`
  - `expression_sql`: the SQL expression text
  - `statement_id`
  - `task_id`
  - `projection_ordinal`

True source-field lineage remains represented only by `DERIVED_FROM`.

## Hive-To-External Sync Tasks

`hive2*` tasks, such as `hive2oracle` and `hive2postgre`, are modeled as data sync/export tasks.
Their output is the non-Hive target table recorded in Horae sync metadata, not the task name.

Table-level facts:

- `ScheduleTask -[:CONSUMES]-> Hive Dataset`
- `ScheduleTask -[:PRODUCES]-> External Dataset`

Column-level facts:

- `External Column -[:DERIVED_FROM]-> Hive Column`

The edge property `target_resolution = "task_sync_target"` indicates that the target table came
from `sync_info["目标库表"]`.

## CTAS, UNION, And Star Expansion

For `CREATE TABLE ... AS SELECT ...`, the created table is used as the column-lineage target when
it can be identified from SQL text. These facts use:

- `target_resolution = "ctas_target"`

For `UNION` / `UNION ALL`, each branch is treated as a separate projection source. Column-lineage
edges include:

- `branch_ordinal`
- `projection_ordinal`

For star projections such as `A.*` and `B.*`, expansion uses, in order:

- registered DMS column metadata;
- inferred CTAS schemas from previous statements in the project;
- unique table-name suffix matching for unqualified table references.

Star-derived edges use:

- `source_resolution = "schema_star_expand"`

## LLM Audit Trail

The LLM layer deliberately separates reusable prompt templates from per-metric runs:

- Prompt templates are stored once as `PromptTemplate` nodes with `template_id`, `version`, and `template_hash`.
- Each metric run stores `PromptRun` metadata: provider, model, status, time, input hash, used template, and used evidence bundle.
- SQL evidence is stored in `llm/evidence_bundles.jsonl`; the graph node keeps counts and hashes, while `EVIDENCES_*` edges connect back to original SQL/table/column facts.
- `CodeDefinition.definition_json` and `DefinitionComparison.comparison_json` preserve structured model output.

This keeps every result reproducible without duplicating the full prompt text on every metric.

## Quality Audits

`audit_graph_facts.py` checks:

- Missing node/edge endpoints.
- Missing required fact properties.
- Isolated nodes.
- Node/edge confidence distributions.
- Node/edge fact type distributions.
- Datasets without columns.
- Metrics without storage or compute task.
- Low-confidence column lineage.

Current `trial_project` audit:

- Missing endpoints: `0`
- Missing required node props: `0`
- Missing required edge props: `0`
- Isolated nodes: `0`
- Metrics without storage: `0`
- Metrics without compute task: `10`
- Low-confidence `DERIVED_FROM`: `0`

Current LLM-enhanced graph:

- Nodes: `56,619`
- Relationships: `104,828`
- `CodeDefinition`: `319`
- `DefinitionComparison`: `319`
- LLM generation failures remaining: `0`
- LLM comparison failures remaining: `0`

Incremental `SourceSnapshot`, `ChangeSet`, and scan state are currently operational JSON artifacts rather than Neo4j nodes. They can be promoted into the graph later if historical change queries become a formal requirement.

## Neo4j Schema

`export_neo4j_schema.py` creates constraints/indexes for:

- `KGNode.id`
- `Dataset.name`
- `ScheduleTask.task_id`
- `SqlStatement.statement_id`
- `Metric.metric_id`
- `Owner.name`
- LLM template, run, evidence, definition, and comparison ids
- Dataset layer/db fields
- Column `(dataset, name)`
- Task type/layer
- Metric Chinese/English names
- Fact `build_id` and `fact_type`
