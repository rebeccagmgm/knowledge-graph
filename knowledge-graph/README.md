# 大数据代码知识图谱

本项目用于从Horae调度元数据、任务页面或运行日志中的SQL、SzConnector表元数据及指标登记信息中，逆向构建代码语义图和业务数据资产图。

## 当前范围

- 从一个或多个结果任务ID递归穿透Horae上游依赖，直至源头。
- 采集任务详情、调度依赖、任务页面SQL/配置及Hive运行日志。
- `hiveTask`、`hiveTask-2.0`以运行日志SQL为准；其他任务以任务页面SQL/配置为准。
- `sparkIndex`从任务页面的`prepare.sqls`识别写入目标，不依赖日志。
- 使用`sqlglot`解析SQL；对带运行噪声的语句提供正则兜底。
- 采集SzConnector DMS表和字段元数据、指标登记信息。
- 构建任务、SQL、表、字段、指标、口径、负责人和数据分层等图事实。
- 保守提取字段血缘，支持明确别名、单一来源表、Schema唯一匹配、星号展开和部分CTE传播。
- 为节点和边记录来源、证据、置信度、构建批次和事实类型。
- 生成Neo4j约束、索引、导入脚本和常用Cypher查询模板。
- 可选调用兼容OpenAI协议的LLM，生成代码优先的指标口径，并与登记口径比较。

Git设计态代码采集和数据服务层暂未纳入当前版本。

## 双层图模型

底层为代码语义图：

```text
ScheduleTask -> RuntimeLog / SqlStatement -> Dataset -> Column
```

上层为业务与数据资产图：

```text
Project -> Metric -> MetricDefinition / CodeDefinition / DefinitionComparison
                 -> Dataset -> DataLayer / Owner
```

两层通过`READS`、`WRITES`、`PRODUCES`、`CONSUMES`、`COMPUTED_BY`、`STORED_IN`、`DERIVED_FROM`等关系连接。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Horae和SzConnector CLI及其认证信息由运行环境提供，不应提交到代码仓库。

## 配置

推荐通过环境变量配置：

```bash
export HORAE_COOKIE="..."
export SZCONNECTOR_COOKIE="..."
export SZCONNECTOR_TOKEN="..."
export KG_OUTPUT_ROOT="$PWD/artifacts/projects"
export KG_LINEAGE_ROOT="$PWD/artifacts/lineage_batch"
```

LLM和Neo4j为可选配置：

```bash
export LLM_API_KEY="..."
export LLM_BASE_URL="https://api.deepseek.com"
export LLM_MODEL="deepseek-v4-pro"
export NEO4J_PASSWORD_FILE="/安全目录/neo4j_password.txt"
```

不要提交Cookie、Token、API Key、Neo4j密码或SSH私钥。

## 项目级流水线

多个结果任务：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174
```

从文件读取任务ID：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --task-file examples/task_ids.example.txt
```

只预览命令，不访问内部服务：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --tasks 236334,212769,207174 \
  --dry-run
```

构建LLM口径层：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id new_project \
  --task-file examples/task_ids.example.txt \
  --build-llm \
  --llm-provider openai-compatible
```

在本机Neo4j运行时，可增加`--import-neo4j`。同时启用LLM时，导入增强后的`strategy_llm`图。

## 主要产物

- `lineage.json`：完整上游调度依赖。
- `task_details.json`：Horae任务详情。
- `code_artifacts_page.json`：任务页面SQL和配置。
- `log_artifacts_full.json`：Hive任务运行日志代码。
- `strategy_sql_statements.json`：按任务类型策略选择后的SQL。
- `strategy_dataset_edges.json`：SQL读写表事实。
- `strategy_column_lineage.json`：字段血缘事实。
- `strategy_graph_nodes.jsonl`、`strategy_graph_edges.jsonl`：基础图事实。
- `strategy_fact_audit.json`：事实完整性和连通性审计。
- `strategy_graph_query_validation.json`：离线查询验证结果。
- `strategy_quality_report.json`：采集、解析、构图和验证质量报告。
- `llm/evidence_bundles.jsonl`：按指标组织的SQL、表、字段和登记口径证据。
- `llm/code_definitions.jsonl`：代码优先的指标口径。
- `llm/definition_comparisons.jsonl`：代码口径与登记口径比较结果。
- `strategy_llm_graph_nodes.jsonl`、`strategy_llm_graph_edges.jsonl`：LLM增强图事实。

所有真实采集产物都应保存在制品目录或对象存储中，不应提交到Git。

## 验证

离线验证图查询：

```bash
python3 kg_probe/validate_graph_queries.py \
  artifacts/projects/new_project \
  --prefix strategy
```

导入并验证Neo4j：

```bash
python3 kg_probe/import_and_validate_neo4j.py \
  artifacts/projects/new_project \
  --prefix strategy \
  --password-file "$NEO4J_PASSWORD_FILE" \
  --batch-size 2000
```

## 已知限制

- 字段血缘采用保守策略，无法确认来源时不会强行连边。
- `select *`展开依赖准确的DMS表字段元数据。
- 多语句临时表、复杂动态SQL和变量替换仍需更强的中间表示。
- 运行日志可能包含引擎噪声或不完整DDL，表级血缘会尽量由正则兜底保留。
- 指标登记信息不是绝对事实；冲突时以代码和表元数据为准。
- LLM结论需要保留证据和模型版本，不应直接替代人工治理。
- 当前尚未建设数据服务节点和正式查询服务层。
- 当前Neo4j导入为全量替换，生产化前需补充增量更新和回滚机制。

## 已规避的流水线问题

- SQL合并和字段血缘尚未稳定时，不并行构图。
- 先构建初始图，以任务`PRODUCES`关系辅助识别目标表，再提取字段血缘。
- 字段解析可能发现新表，因此SzConnector元数据需要在字段血缘前后分别补采。
- 大项目使用批量刷盘，避免每查询一张表就重写完整DMS文件。
- 只有Hive任务读取运行日志；其他任务读取任务页面代码。
- Neo4j导入保持可选，离线阶段也能完成事实审计和查询验证。

更完整的图模型见[GRAPH_MODEL.md](GRAPH_MODEL.md)，流水线说明见[PROJECT_PIPELINE.md](PROJECT_PIPELINE.md)。

总体技术方案见[TECHNICAL_SOLUTION.md](TECHNICAL_SOLUTION.md)，增量更新操作见[INCREMENTAL_UPDATE.md](INCREMENTAL_UPDATE.md)。

查询层统一协议和12个查询原语见[QUERY_LAYER_DESIGN.md](QUERY_LAYER_DESIGN.md)。

查询层CLI和Python调用示例见[QUERY_LAYER_USAGE.md](QUERY_LAYER_USAGE.md)。
