# 知识图谱事实模型

## 分层边界

当前原型将端到端链路拆分为：

- 采集层：原始任务、日志、SQL/配置、DMS 表元数据和指标登记产物。
- 解析层：标准化 SQL 语句、表级血缘事实和字段级血缘事实。
- 事实层：面向图谱的实体与关系，附带来源、置信度和证据元数据。
- 图谱层：Neo4j schema、JSONL 图事实、导入脚本、验证查询和 Cypher 模板。
- LLM 口径层：可选的指标证据包、Prompt/模型调用、代码优先口径，以及与登记口径的比较结果。

## 通用事实属性

每个图节点和图边都会补充以下属性：

- `fact_type`：语义类别，例如 `table_lineage`、`column_lineage`、`business_metric` 或 `schedule_task`。
- `project_key`：项目或根血缘标识。
- `graph_prefix`：事实集前缀，例如 `strategy`。
- `build_id`：图谱构建批次标识。
- `built_at`：构建时间。
- `confidence`：`high`、`medium`、`low` 或 `unknown`。
- `inferred`：该事实是否由推断得到，而不是直接采集得到。

图边还会额外包含：

- `evidence_from_id`
- `evidence_to_id`

部分特定边类型还会携带 SQL statement、任务、来源解析方式或目标解析方式等证据属性。

## 核心节点

- `Project`：一次知识图谱项目快照。
- `ScheduleTask`：Horae 调度任务。
- `RuntimeLog`：运行日志产物。
- `SqlStatement`：标准化 SQL 语句产物。
- `Dataset`：表或数据资产。
- `Column`：数据集字段。
- `Metric`：指标登记中的指标。
- `MetricDefinition`：指标登记口径文本。
- `Owner`：负责人、开发人或设计人。
- `DataLayer`：`odata`、`pdata`、`dm_index_n`、`dm`、`other`、`root_unknown`。
- `EvidenceBundle`：按指标组装的证据包，包含图事实、SQL 片段、读写表、目标字段和登记口径文本。
- `PromptTemplate`：带版本的 Prompt 模板。模板正文只保存一次，并记录 hash。
- `PromptRun`：一次模型调用或 mock 调用，用于口径生成或口径比较。
- `ModelVersion`：一次 PromptRun 使用的模型提供方和模型标识。
- `CodeDefinition`：基于 SQL 和图证据生成的代码优先指标口径。
- `DefinitionComparison`：代码口径与登记口径之间的比较结果。

## 核心关系

调度关系：

- `Project -[:HAS_ENTRY_TASK]-> ScheduleTask`
- `downstream ScheduleTask -[:DEPENDS_ON]-> upstream ScheduleTask`

任务与数据：

- `ScheduleTask -[:PRODUCES]-> Dataset`
- `ScheduleTask -[:CONSUMES]-> Dataset`
- `ScheduleTask -[:HAS_RUNTIME_LOG]-> RuntimeLog`
- `ScheduleTask -[:EMITS_SQL]-> SqlStatement`

SQL 与表级血缘：

- `SqlStatement -[:READS]-> Dataset`
- `SqlStatement -[:WRITES]-> Dataset`
- `target Dataset -[:DATASET_DEPENDS_ON]-> source Dataset`

字段血缘：

- `Dataset -[:HAS_COLUMN]-> Column`
- `target Column -[:DERIVED_FROM]-> source Column`
- `target Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression`

指标与业务：

- `Metric -[:STORED_IN]-> Dataset`
- `Metric -[:COMPUTED_BY]-> ScheduleTask`
- `Metric -[:HAS_DEFINITION]-> MetricDefinition`
- `Metric -[:HAS_EVIDENCE_BUNDLE]-> EvidenceBundle`
- `Metric -[:HAS_CODE_DEFINITION]-> CodeDefinition`

LLM 证据与生成：

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

负责人和数据分层：

- `Owner -[:OWNS]-> ScheduleTask | Dataset | Metric`
- `Dataset | ScheduleTask -[:BELONGS_TO_LAYER]-> DataLayer`

## 置信度规则

高置信度：

- Horae 直接采集到的调度依赖。
- DMS 直接采集到的表和字段元数据。
- 指标登记中直接采集到的存储表和登记口径事实。
- SQLGlot 解析得到的 `READS` / `WRITES`。
- 来自显式表别名的字段血缘。

中置信度：

- 由调度依赖和任务产出推断出的表依赖。
- 由单一读表上下文、schema 唯一字段匹配、星号展开或 CTE 传播得到的字段血缘。
- 由指标存储表生产任务推断出的指标计算任务。
- 由元数据或命名规则推导出的负责人和分层。
- 真实模型调用生成的 LLM 输出，除非后续经过人工确认提升置信度。

低置信度：

- mock LLM 生成的口径和比较结果。
- 预留给未解析或弱推断事实。当前图构建不会输出低置信度的 `DERIVED_FROM` 边。

## 生成字段值

有些目标字段不是从来源表字段派生的，例如固定值、空占位、分区值和运行时表达式：

- 固定值：`'RCC' AS data_src_cd`
- 空占位：`'' AS remark`
- 运行时表达式：`from_unixtime(unix_timestamp()) AS data_time`

这类字段会被显式建模，而不是计入字段血缘错误：

- 节点：`GeneratedExpression`
- 关系：`Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression`
- 关键属性：
  - `generation_type`：`literal`、`generated_expression` 或 `system_expression`
  - `expression_sql`：SQL 表达式文本
  - `statement_id`
  - `task_id`
  - `projection_ordinal`

真实的来源字段血缘仍只使用 `DERIVED_FROM` 表达。

## Hive 到外部库表同步任务

`hive2*` 任务，例如 `hive2oracle` 和 `hive2postgre`，建模为数据同步或数据出口任务。其产出是 Horae 同步元信息中登记的非 Hive 目标表，而不是任务名。

表级事实：

- `ScheduleTask -[:CONSUMES]-> Hive Dataset`
- `ScheduleTask -[:PRODUCES]-> External Dataset`

字段级事实：

- `External Column -[:DERIVED_FROM]-> Hive Column`

边属性 `target_resolution = "task_sync_target"` 表示目标表来自 `sync_info["目标库表"]`。

## CTAS、UNION 与星号展开

对于 `CREATE TABLE ... AS SELECT ...`，当能从 SQL 文本中识别创建表时，该创建表会作为字段血缘目标。此类事实使用：

- `target_resolution = "ctas_target"`

对于 `UNION` / `UNION ALL`，每个分支会被视为独立的 projection 来源。字段血缘边会包含：

- `branch_ordinal`
- `projection_ordinal`

对于 `A.*` 和 `B.*` 等星号 projection，展开优先级如下：

- 已登记的 DMS 字段元数据。
- 从项目中前序 CTAS 语句推断出的 schema。
- 针对未带库名表引用的唯一表名后缀匹配。

由星号展开得到的边使用：

- `source_resolution = "schema_star_expand"`

## LLM 审计链路

LLM 口径层刻意将可复用 Prompt 模板与每个指标的实际调用分开：

- Prompt 模板保存为 `PromptTemplate` 节点，包含 `template_id`、`version` 和 `template_hash`。
- 每次指标调用保存 `PromptRun` 元数据：提供方、模型、状态、时间、输入 hash、使用的模板和使用的证据包。
- SQL 证据保存在 `llm/evidence_bundles.jsonl` 中；图节点只保留数量和 hash，同时通过 `EVIDENCES_*` 边连接回原始 SQL、表和字段事实。
- `CodeDefinition.definition_json` 和 `DefinitionComparison.comparison_json` 保留结构化模型输出。

这样可以保证每个结果可追溯、可复现，同时避免在每个指标上重复保存完整 Prompt 正文。

## 质量审计

`audit_graph_facts.py` 检查：

- 节点或边端点缺失。
- 必填事实属性缺失。
- 孤立节点。
- 节点和边的置信度分布。
- 节点和边的事实类型分布。
- 缺少字段的表。
- 缺少存储表或计算任务的指标。
- 低置信度字段血缘。

当前 `trial_project` 审计结果：

- 缺失端点：`0`
- 缺失必填节点属性：`0`
- 缺失必填边属性：`0`
- 孤立节点：`0`
- 缺少存储表的指标：`0`
- 缺少计算任务的指标：`10`
- 低置信度 `DERIVED_FROM`：`0`

当前 LLM 增强图：

- 节点：`56,619`
- 关系：`104,828`
- `CodeDefinition`：`319`
- `DefinitionComparison`：`319`
- 剩余 LLM 口径生成失败：`0`
- 剩余 LLM 口径比较失败：`0`

增量更新中的 `SourceSnapshot`、`ChangeSet` 和扫描状态当前仍是运行态 JSON 产物，而不是 Neo4j 节点。如果后续需要正式查询历史变化，可以再提升为图节点。

## Neo4j Schema

`export_neo4j_schema.py` 会为以下对象创建约束或索引：

- `KGNode.id`
- `Dataset.name`
- `ScheduleTask.task_id`
- `SqlStatement.statement_id`
- `Metric.metric_id`
- `Owner.name`
- LLM 模板、运行、证据、口径和比较 ID
- Dataset 的 layer/db 字段
- Column 的 `(dataset, name)`
- Task 的 type/layer
- Metric 的中文名和英文名
- 事实的 `build_id` 和 `fact_type`
