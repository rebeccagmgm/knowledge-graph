# 知识图谱事实模型

## 分层边界

原型工程将链路拆成五层：

- 采集层：保存原始任务、日志、SQL/配置、DMS、指标登记等采集产物。
- 解析层：生成规范化 SQL 语句、表级血缘和字段级血缘事实。
- 事实层：将实体和关系整理成可入图事实，并补充来源、置信度和证据元数据。
- 图谱层：维护 Neo4j schema、JSONL 图事实、导入脚本、校验查询和 Cypher 模板。
- LLM 口径层：可选构建指标证据包、Prompt/模型调用记录、代码优先指标口径和登记口径比对结果。

## 通用事实属性

所有图节点和边都会补充以下属性：

- `fact_type`：语义类别，例如 `table_lineage`、`column_lineage`、`business_metric`、`schedule_task`。
- `project_key`：项目或根血缘标识。
- `graph_prefix`：事实集合前缀，例如 `strategy`。
- `build_id`：图谱构建批次标识。
- `built_at`：构建时间。
- `confidence`：置信度，取值为 `high`、`medium`、`low` 或 `unknown`。
- `inferred`：是否为推断事实，而不是直接采集事实。

边会额外包含：

- `evidence_from_id`
- `evidence_to_id`

部分边类型还会携带 SQL 语句、调度任务、源字段解析方式或目标表解析方式等证据属性。

## 核心节点

- `Project`：一个项目图谱快照。
- `ScheduleTask`：Horae 调度任务。
- `RuntimeLog`：运行日志产物。
- `SqlStatement`：规范化 SQL 语句。
- `Dataset`：表或数据资产。
- `Column`：表字段。
- `Metric`：指标登记系统中的指标。
- `MetricDefinition`：指标登记口径文本。
- `Owner`：负责人、开发人、设计人等。
- `DataLayer`：数据层级，包括 `odata`、`pdata`、`dm_index_n`、`dm`、`other`、`root_unknown`。
- `GeneratedExpression`：没有真实源字段的生成字段表达式。
- `EvidenceBundle`：每个指标的证据包，由图事实、SQL 片段、读写表、目标字段和登记口径组成。
- `PromptTemplate`：版本化 Prompt 模板。模板正文只存一份，并记录哈希。
- `PromptRun`：一次真实模型调用或 mock 调用。
- `ModelVersion`：PromptRun 使用的 provider/model 标识。
- `CodeDefinition`：基于 SQL 和图证据生成的代码优先指标口径。
- `DefinitionComparison`：代码口径与登记口径的比对结果。

## 核心边

调度：

- `Project -[:HAS_ENTRY_TASK]-> ScheduleTask`
- `downstream ScheduleTask -[:DEPENDS_ON]-> upstream ScheduleTask`

任务和数据：

- `ScheduleTask -[:PRODUCES]-> Dataset`
- `ScheduleTask -[:CONSUMES]-> Dataset`
- `ScheduleTask -[:HAS_RUNTIME_LOG]-> RuntimeLog`
- `ScheduleTask -[:EMITS_SQL]-> SqlStatement`

SQL 和表血缘：

- `SqlStatement -[:READS]-> Dataset`
- `SqlStatement -[:WRITES]-> Dataset`
- `target Dataset -[:DATASET_DEPENDS_ON]-> source Dataset`

字段血缘：

- `Dataset -[:HAS_COLUMN]-> Column`
- `target Column -[:DERIVED_FROM]-> source Column`
- `target Column -[:INFLUENCED_BY]-> source Column`
- `target Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression`

指标和业务：

- `Metric -[:STORED_IN]-> Dataset`
- `Metric -[:COMPUTED_BY]-> ScheduleTask`
- `Metric -[:HAS_DEFINITION]-> MetricDefinition`
- `Metric -[:HAS_EVIDENCE_BUNDLE]-> EvidenceBundle`
- `Metric -[:HAS_CODE_DEFINITION]-> CodeDefinition`

LLM 证据和生成：

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

归属和分层：

- `Owner -[:OWNS]-> ScheduleTask | Dataset | Metric`
- `Dataset | ScheduleTask -[:BELONGS_TO_LAYER]-> DataLayer`

## 置信度规则

高置信度：

- Horae 直接调度依赖。
- DMS 直接表/字段元数据。
- 指标登记系统中的存储表和登记口径。
- SQLGlot 解析出的 `READS` / `WRITES`。
- 来自显式表别名的字段血缘。

中置信度：

- 根据调度依赖和产出表推断的数据集依赖。
- 根据单读表上下文、schema 唯一字段匹配、星号展开或 CTE 传播得到的字段血缘。
- 根据指标存储表生产任务推断的指标计算任务。
- 根据元数据或命名规则推断的负责人、数据层级。
- 真实模型生成的 LLM 口径，除非后续被人工确认提升置信度。

低置信度：

- mock 模型生成的口径和比对。
- 为弱推断或未解决事实预留。目前图构建不会输出低置信度 `DERIVED_FROM` 边。

## 生成字段值

部分目标字段不是来自源表字段，例如固定值、空字符串占位、分区值和运行时表达式：

- `'RCC' AS data_src_cd`
- `'' AS remark`
- `from_unixtime(unix_timestamp()) AS data_time`

这些字段不会被当作字段血缘解析错误，而是显式建模：

- 节点：`GeneratedExpression`
- 边：`Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression`
- 关键属性：
  - `generation_type`：`literal`、`generated_expression` 或 `system_expression`
  - `expression_sql`
  - `statement_id`
  - `task_id`
  - `projection_ordinal`

真实源字段血缘仍只通过 `DERIVED_FROM` 表示。

## 间接字段影响

`DERIVED_FROM` 只表示目标字段值的直接来源。部分字段虽然不直接构成输出字段的值，但会影响输出结果集或计算范围，例如：

- `WHERE` 过滤条件。
- `JOIN ON` 或 `USING` 关联条件。
- `GROUP BY` 分组字段。
- `HAVING` 过滤条件。
- `QUALIFY` 过滤条件。
- `ORDER BY` 排序字段。

这些字段被建模为：

- 边：`target Column -[:INFLUENCED_BY]-> source Column`
- 关键属性：
  - `influence_type`：`filter`、`join_condition`、`join_using`、`group_by`、`having`、`qualify`、`order_by`
  - `expression_sql`
  - `statement_id`
  - `task_id`
  - `branch_ordinal`

这样字段影响分析可以区分“字段值直接来自某源字段”和“字段受到过滤、关联、分组等条件间接影响”。第一版不改变 `DERIVED_FROM` 语义，而是新增 `INFLUENCED_BY` 作为补充关系。

## Hive 到外部库同步任务

`hive2*` 类任务，例如 `hive2oracle`、`hive2postgre`，被建模为数据同步/导出任务。它们的产出不是任务名，而是 Horae 同步元数据中记录的非 Hive 目标表。

表级事实：

- `ScheduleTask -[:CONSUMES]-> Hive Dataset`
- `ScheduleTask -[:PRODUCES]-> External Dataset`

字段级事实：

- `External Column -[:DERIVED_FROM]-> Hive Column`

边属性 `target_resolution = "task_sync_target"` 表示目标表来自 `sync_info["目标库表"]`。

## CTAS、UNION 和星号展开

对于 `CREATE TABLE ... AS SELECT ...`，如果能从 SQL 文本识别出创建表，则将该表作为字段血缘目标。相关事实使用：

- `target_resolution = "ctas_target"`

对于 `UNION` / `UNION ALL`，每个分支会作为独立投影来源处理。字段血缘边包含：

- `branch_ordinal`
- `projection_ordinal`

对于 `A.*`、`B.*` 等星号投影，展开顺序为：

- DMS 登记字段元数据。
- 项目内前序 CTAS 语句推断出的 schema。
- 对未限定表引用做唯一表名后缀匹配。

星号展开产生的边使用：

- `source_resolution = "schema_star_expand"`

## LLM 审计链路

LLM 口径层刻意区分可复用 Prompt 模板和每个指标的实际调用：

- Prompt 模板以 `PromptTemplate` 节点保存，包含 `template_id`、`version` 和 `template_hash`。
- 每个指标调用保存为 `PromptRun`，记录 provider、model、状态、时间、输入哈希、使用的模板和证据包。
- SQL 证据保存在 `llm/evidence_bundles.jsonl`；图节点保存数量和哈希，`EVIDENCES_*` 边连接回原始 SQL、表、字段事实。
- `CodeDefinition.definition_json` 和 `DefinitionComparison.comparison_json` 保留结构化模型输出。

这样可以复现每次结果，同时避免在每个指标节点上重复保存完整 Prompt 正文。

## 质量审计

`audit_graph_facts.py` 检查：

- 节点/边缺失端点。
- 必填事实属性缺失。
- 孤立节点。
- 节点/边置信度分布。
- 节点/边事实类型分布。
- 缺少字段的表。
- 缺少存储表或计算任务的指标。
- 低置信度字段血缘。

当前典型 LLM 增强图审计结果：

- 缺失端点：`0`
- 缺失必填节点属性：`0`
- 缺失必填边属性：`0`
- 孤立节点：`0`
- LLM 生成失败剩余：`0`
- LLM 比对失败剩余：`0`

增量更新中的 `SourceSnapshot`、`ChangeSet` 和扫描状态目前是运行态 JSON 产物，尚未提升为 Neo4j 节点。如果后续需要查询历史变更，可再正式入图。

## Neo4j Schema

`export_neo4j_schema.py` 会为以下字段创建约束或索引：

- `KGNode.id`
- `Dataset` 的项目内表名唯一约束
- `ScheduleTask` 的项目内任务 ID 唯一约束
- `SqlStatement` 的项目内语句 ID 唯一约束
- `Metric` 的项目内指标 ID 唯一约束
- `Owner` 的项目内名称唯一约束
- LLM 模板、调用、证据、口径和比对 ID
- Dataset 层级和库名字段
- Column 的 `(dataset, name)`
- Task 类型和层级
- Metric 中文名和英文名
- 事实 `build_id` 和 `fact_type`
- `KGNode` 全文索引 `kg_entity_search`
