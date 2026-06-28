# 知识图谱事实模型

## 分层边界

系统按职责分为五层：

1. **采集层**：保存任务、依赖、日志、页面SQL/配置、DMS元数据和指标登记原始产物。
2. **解析层**：生成规范化SQL、表读写关系和字段血缘事实。
3. **事实层**：形成带来源、证据、置信度和构建信息的图节点与边。
4. **图谱层**：生成JSONL图事实、Neo4j Schema、导入脚本、验证查询和Cypher模板。
5. **LLM口径层**：构建证据包，记录Prompt及模型调用，生成代码口径并与登记口径比较。

## 通用事实属性

每个节点和边都包含：

- `fact_type`：事实语义类型，如`table_lineage`、`column_lineage`、`business_metric`。
- `project_key`：所属项目或根血缘标识。
- `graph_prefix`：事实集前缀，如`strategy`、`strategy_llm`。
- `build_id`：本次构建标识。
- `built_at`：构建时间。
- `confidence`：规范化为`high`、`medium`、`low`。
- `inferred`：是否由规则或推理得出。

边还可包含`evidence_from_id`、`evidence_to_id`及SQL、任务、来源解析和目标解析证据。

## 核心节点

- `Project`：一个知识图谱项目快照。
- `ScheduleTask`：Horae调度任务。
- `RuntimeLog`：运行日志证据。
- `SqlStatement`：规范化SQL语句。
- `Dataset`：表或数据资产。
- `Column`：表字段。
- `Metric`：业务指标。
- `MetricDefinition`：指标登记口径。
- `Owner`：任务、表或指标负责人。
- `DataLayer`：`odata`、`pdata`、`dm_index_n`、`dm`、`other`、`root_unknown`。
- `EvidenceBundle`：某个指标对应的SQL、表、字段和登记口径证据包。
- `PromptTemplate`：带版本和Hash的Prompt模板，模板文本只保存一次。
- `PromptRun`：一次口径生成或比较调用。
- `ModelVersion`：调用使用的提供方和模型版本。
- `CodeDefinition`：根据代码与表元数据生成的指标口径。
- `DefinitionComparison`：代码口径与登记口径的比较结论。

## 核心关系

调度关系：

```text
Project -[:HAS_ENTRY_TASK]-> ScheduleTask
下游ScheduleTask -[:DEPENDS_ON]-> 上游ScheduleTask
```

任务与数据关系：

```text
ScheduleTask -[:PRODUCES]-> Dataset
ScheduleTask -[:CONSUMES]-> Dataset
ScheduleTask -[:HAS_RUNTIME_LOG]-> RuntimeLog
ScheduleTask -[:EMITS_SQL]-> SqlStatement
```

SQL和表血缘：

```text
SqlStatement -[:READS]-> Dataset
SqlStatement -[:WRITES]-> Dataset
目标Dataset -[:DATASET_DEPENDS_ON]-> 来源Dataset
```

字段血缘：

```text
Dataset -[:HAS_COLUMN]-> Column
目标Column -[:DERIVED_FROM]-> 来源Column
```

指标与业务关系：

```text
Metric -[:STORED_IN]-> Dataset
Metric -[:COMPUTED_BY]-> ScheduleTask
Metric -[:HAS_DEFINITION]-> MetricDefinition
Metric -[:HAS_EVIDENCE_BUNDLE]-> EvidenceBundle
Metric -[:HAS_CODE_DEFINITION]-> CodeDefinition
```

LLM证据和生成关系：

```text
EvidenceBundle -[:EVIDENCES_SQL]-> SqlStatement
EvidenceBundle -[:EVIDENCES_DATASET]-> Dataset
EvidenceBundle -[:EVIDENCES_COLUMN]-> Column
CodeDefinition -[:GENERATED_BY]-> PromptRun
PromptRun -[:USED_TEMPLATE]-> PromptTemplate
PromptRun -[:USED_EVIDENCE]-> EvidenceBundle
PromptRun -[:USED_MODEL]-> ModelVersion
CodeDefinition -[:HAS_COMPARISON]-> DefinitionComparison
DefinitionComparison -[:COMPARES_CODE_DEFINITION]-> CodeDefinition
DefinitionComparison -[:COMPARES_REGISTERED_DEFINITION]-> MetricDefinition
```

归属与分层：

```text
Owner -[:OWNS]-> ScheduleTask | Dataset | Metric
Dataset | ScheduleTask -[:BELONGS_TO_LAYER]-> DataLayer
```

## 置信度规则

### 高置信度

- Horae直接调度依赖。
- DMS直接提供的表和字段元数据。
- 指标登记中的存储位置和登记文本。
- `sqlglot`成功解析的`READS`、`WRITES`。
- 通过明确表别名解析的字段血缘。

### 中置信度

- 根据调度依赖及任务产出推导的表依赖。
- 通过单一来源表、Schema唯一匹配、星号展开或CTE传播得到的字段血缘。
- 根据指标存储表的生产任务推导的`COMPUTED_BY`。
- 通过元数据或命名规则推导的负责人和数据分层。
- 未经人工确认的真实模型输出。

### 低置信度

- Mock模式生成的口径和比较结果。
- 其他未解决或弱推断事实。当前构图不会生成低置信度`DERIVED_FROM`边。

## LLM审计链

- Prompt模板作为`PromptTemplate`节点保存一次，记录`template_id`、`version`和`template_hash`。
- 每次模型调用保存为`PromptRun`，记录提供方、模型、状态、时间、输入Hash及所用模板和证据包。
- SQL证据保存在`llm/evidence_bundles.jsonl`，图中用`EVIDENCES_*`边回连原始SQL、表和字段事实。
- `CodeDefinition.definition_json`和`DefinitionComparison.comparison_json`保留模型结构化原始输出。
- 调用失败可以保留为`PromptRun`审计记录，但不得生成`CodeDefinition`或比较事实。

这样既避免每个指标重复保存完整Prompt，也能重现每条模型结论。

## 质量审计

`audit_graph_facts.py`检查：

- 节点和边端点是否缺失。
- 必填事实属性是否缺失。
- 是否存在孤立节点。
- 节点、边的置信度和事实类型分布。
- 是否存在缺少字段的表。
- 指标是否缺少存储表或计算任务。
- 是否存在低置信度字段血缘。

## Neo4j Schema

`export_neo4j_schema.py`为以下字段生成约束或索引：

- `KGNode.id`
- `Dataset.name`
- `ScheduleTask.task_id`
- `SqlStatement.statement_id`
- `Metric.metric_id`
- `Owner.name`
- LLM模板、调用、证据、代码口径和比较标识
- 表的分层和库名
- 字段的`(dataset, name)`组合
- 任务类型和分层
- 指标中英文名称
- 事实的`build_id`和`fact_type`

