# 大数据代码知识图谱探针

本目录是代码逆向生成知识图谱的本地原型工程，用于从 Horae 调度元信息、任务页面 SQL/配置、运行日志、SzConnector 表元数据和指标登记信息中构建项目级知识图谱。

## 当前状态

项目已经进入“可复用原型”阶段，当前能力包括：

- 支持从多个结果任务 ID 反向穿透上游调度链路。
- 支持多项目图谱共存导入 Neo4j，并通过 `project_id` 隔离查询。
- 支持 SQL 表级血缘、字段级血缘、调度依赖、指标登记口径和 LLM 代码口径入图。
- 支持基于项目登记表的增量扫描，默认扫描间隔为 48 小时。
- 支持 HTTP 查询接口，面向后续问答机器人、智能体和项目组同事查询。

当前已经验证的典型项目包括：

- `digital_operations`：数字化运营
- `project_customer_report`：客户报告
- `project_stastic_month`：统计月报
- `t0`：t0
- `project_sale_new`：交叉销售
- `sale`：交叉销售（全）

完整技术方案和路线图见 `TECHNICAL_SOLUTION.md`。

## 当前范围

- 从一个或多个结果任务 ID 开始，调用 Horae 穿透上游任务依赖。
- 采集任务详情、调度依赖、任务页面 SQL/配置。
- 对 `hiveTask`、`hiveTask-2.0` 采集运行日志，并从日志中提取 SQL。
- 对非 Hive 类任务优先使用任务页面 SQL/配置。
- 使用 `sqlglot` 解析 SQL，并对运行日志中的噪声和异常 SQL 片段做保守兜底。
- 统一策略合并 SQL：
  - `hiveTask` / `hiveTask-2.0`：以运行日志 SQL 为准。
  - 其他任务类型：以任务页面 SQL/配置为准。
- 采集 SzConnector DMS 表元数据和指标登记信息。
- 构建图谱 JSONL 节点、边，以及 Neo4j 导入脚本。
- 抽取保守字段血缘，并物化表级依赖边。
- 支持 `*` / `alias.*` 投影展开，前提是能通过 DMS 或推断 schema 明确源表字段。
- 支持 CTAS、UNION、CTE 投影、单读表上下文和 schema 唯一匹配等保守字段解析。
- 为事实节点和边补充来源、置信度、构建批次、事实类型等元数据。
- 审计图谱事实的必填属性、缺失端点、孤立节点、置信度分布、指标/表覆盖情况。
- 导出 Neo4j 约束、索引、常用 Cypher 模板。
- Neo4j 导入前支持离线查询校验。
- 可选构建 LLM 指标口径层：组织证据、生成代码优先口径、比对登记口径，并合并进增强图。

暂不做 Git 仓库设计态代码采集；数据服务层也暂时保留为后续扩展。

## 主流水线

项目级多根任务流水线：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --import-neo4j
```

任务文件支持逗号分隔或一行一个 ID：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --task-file /path/to/task_ids.txt \
  --import-neo4j
```

只预览将要执行的命令，不调用内部服务：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --dry-run
```

构建可选 LLM 口径层，默认 `mock` 模式不调用真实模型：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --build-llm
```

调用 OpenAI 兼容模型：

```bash
export OPENAI_API_KEY="..."
export LLM_MODEL="deepseek-v4-pro"
export LLM_BASE_URL="https://api.deepseek.com"
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --build-llm \
  --llm-provider openai-compatible
```

如果同时使用 `--build-llm` 和 `--import-neo4j`，默认导入增强图前缀 `strategy_llm`。

单根任务旧入口仍保留：

```bash
PYTHONPATH=/Applications/personal-work/kg-local-pydeps \
python3 /Applications/personal-work/kg_probe/run_pipeline.py 238758
```

## 关键产物

- `lineage.json`：合并后的上游任务血缘。
- `task_details.json`：Horae 任务详情。
- `code_artifacts_page.json`：任务页面 SQL/配置。
- `log_artifacts_full.json`：Hive 类任务运行日志。
- `strategy_sql_statements.json`：按策略合并后的 SQL 语句。
- `strategy_dataset_edges.json`：SQL READ/WRITE 表级事实。
- `strategy_column_lineage.json`：字段血缘事实。
- `strategy_graph_nodes.jsonl`：图谱节点。
- `strategy_graph_edges.jsonl`：图谱边。
- `strategy_neo4j_import.cypher`：Neo4j 导入脚本。
- `strategy_neo4j_schema.cypher`：Neo4j 约束和索引。
- `strategy_query_templates.cypher`：可复用 Cypher 查询模板。
- `strategy_quality_report.json`：采集、解析、图谱和校验报告。
- `strategy_graph_query_validation.json`：离线图查询校验样本。
- `strategy_fact_audit.json`：事实完整性、来源和连通性审计。
- `llm/evidence_bundles.jsonl`：每个指标发送给 LLM 的 SQL、表、字段、登记口径证据。
- `llm/code_definition_requests.jsonl`：渲染后的 LLM 请求，包含模板 ID、版本和哈希。
- `llm/code_definitions.jsonl`：LLM 生成的代码优先指标口径。
- `llm/definition_comparisons.jsonl`：代码口径与登记口径的比对结果。
- `strategy_llm_graph_nodes.jsonl`：基础图加 LLM 口径事实后的节点。
- `strategy_llm_graph_edges.jsonl`：基础图加 LLM 证据、模型、Prompt 和比对关系后的边。

更多节点、边和置信度规则见 `GRAPH_MODEL.md`。

增量更新说明见 `INCREMENTAL_UPDATE.md`。

接口调用说明见 `QUERY_API_USAGE.md`。

## 文档索引

- `TECHNICAL_SOLUTION.md`：整体架构、当前进度、增量更新和查询层方案。
- `PROJECT_PIPELINE.md`：项目级流水线、断点复用和产物说明。
- `GRAPH_MODEL.md`：节点、边、置信度和审计定义。
- `INCREMENTAL_UPDATE.md`：项目登记、扫描、质量门禁和重建行为。
- `QUERY_LAYER_DESIGN.md`：标准返回协议和 12 个查询原语。
- `QUERY_LAYER_USAGE.md`：查询服务的 CLI/Python 调用示例。
- `QUERY_API_USAGE.md`：HTTP 查询接口调用说明。

## 图模型概览

主要节点：

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
- `GeneratedExpression`

主要边：

- 调度和归属：`HAS_ENTRY_TASK`、`DEPENDS_ON`、`OWNS`
- 任务和数据：`PRODUCES`、`CONSUMES`、`HAS_RUNTIME_LOG`
- SQL 和数据：`EMITS_SQL`、`READS`、`WRITES`
- 物化表血缘：`DATASET_DEPENDS_ON`
- 表结构和指标：`HAS_COLUMN`、`STORED_IN`、`COMPUTED_BY`、`HAS_DEFINITION`
- 字段血缘：`DERIVED_FROM`、`GENERATED_BY_EXPRESSION`
- 分层：`BELONGS_TO_LAYER`
- LLM 口径层：`HAS_EVIDENCE_BUNDLE`、`EVIDENCES_SQL`、`EVIDENCES_DATASET`、`EVIDENCES_COLUMN`、`HAS_CODE_DEFINITION`、`GENERATED_BY`、`USED_TEMPLATE`、`USED_EVIDENCE`、`USED_MODEL`、`HAS_COMPARISON`、`COMPARES_CODE_DEFINITION`、`COMPARES_REGISTERED_DEFINITION`

方向约定：

- `target Dataset -[:DATASET_DEPENDS_ON]-> source Dataset`
- `target Column -[:DERIVED_FROM]-> source Column`
- `downstream ScheduleTask -[:DEPENDS_ON]-> upstream ScheduleTask`

## 校验

离线图查询校验：

```bash
python3 /Applications/personal-work/kg_probe/validate_graph_queries.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy
```

导出查询模板：

```bash
python3 /Applications/personal-work/kg_probe/export_query_templates.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy
```

只为已有图构建 LLM 口径层：

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

## Neo4j 导入

批量导入并校验：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kg-pycache \
python3 /Applications/personal-work/kg_probe/import_and_validate_neo4j.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy \
  --batch-size 2000
```

多项目导入建议使用 `--project-id` 和 `--replace-project`，只替换当前项目：

```bash
python3 /Applications/personal-work/kg_probe/import_and_validate_neo4j.py \
  /path/to/project_dir \
  --prefix strategy_llm \
  --project-id sale \
  --replace-project
```

只校验，不重新导入：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kg-pycache \
python3 /Applications/personal-work/kg_probe/import_and_validate_neo4j.py \
  /Applications/personal-work/kg-code-snapshots/projects/trial_project \
  --prefix strategy \
  --skip-import
```

## 已知限制

- 字段血缘刻意保守。未限定字段只有在表别名、单读表上下文或 DMS schema 唯一匹配能消歧时才解析源字段。
- `select *` 展开依赖 DMS 精确表字段，缺少精确字段元数据时不会盲目展开。
- CTE 血缘目前覆盖保守投影传播，多语句临时表链和动态 SQL 变量还需要更强的中间表示。
- 运行日志中包含执行引擎噪声或 DDL 片段时，部分 SQL 解析错误是预期现象；表级血缘会尽量通过兜底规则保留。
- 数据服务层尚未建模，后续可在上层业务/服务图中扩展。
