# 项目级流水线说明

`run_project_pipeline.py` 是面向多结果任务 ID 项目的统一入口。它把采集、解析、事实构建、质量审计、Neo4j 导入和可选 LLM 口径层串成一条可复用流水线。

## 基本用法

直接传入任务 ID：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --tasks 236334,212769,207174 \
  --import-neo4j
```

使用任务文件，支持逗号分隔或一行一个 ID：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file /Applications/personal-work/my_project_tasks.txt \
  --import-neo4j
```

只预览命令，不真正调用内部服务：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --tasks 236334,212769,207174 \
  --dry-run
```

构建可选 LLM 指标口径层：

```bash
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file /Applications/personal-work/my_project_tasks.txt \
  --build-llm
```

真实 LLM 调用使用 OpenAI 兼容接口：

```bash
export OPENAI_API_KEY="..."
export LLM_MODEL="deepseek-v4-pro"
export LLM_BASE_URL="https://api.deepseek.com"
python3 /Applications/personal-work/kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file /Applications/personal-work/my_project_tasks.txt \
  --build-llm \
  --llm-provider openai-compatible
```

默认 LLM provider 是 `mock`，用于验证证据组织、请求生成和入图流程，不会调用真实模型。

## 输出目录

对于 `--project-id my_project`，主要输出在：

```text
/Applications/personal-work/kg-code-snapshots/lineage_batch/my_project
/Applications/personal-work/kg-code-snapshots/projects/my_project
```

重要最终产物：

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
- `strategy_neo4j_validation.json`，仅在使用 `--import-neo4j` 时生成或更新。
- `strategy_quality_report.json`
- `llm/evidence_bundles.jsonl`，仅在使用 `--build-llm` 时生成。
- `llm/code_definition_requests.jsonl`，仅在使用 `--build-llm` 时生成。
- `llm/code_definitions.jsonl`，仅在使用 `--build-llm` 时生成。
- `llm/definition_comparisons.jsonl`，仅在使用 `--build-llm` 时生成。
- `strategy_llm_graph_nodes.jsonl`，仅在使用 `--build-llm` 时生成。
- `strategy_llm_graph_edges.jsonl`，仅在使用 `--build-llm` 时生成。
- `project_pipeline_manifest.json`

## 执行顺序

1. 对每个结果任务采集上游调度血缘。
2. 合并多个根任务的血缘快照，形成项目级血缘图。
3. 采集 Horae 任务详情。
4. 采集任务页面 SQL/配置。
5. 只对 `hiveTask,hiveTask-2.0` 采集运行日志。
6. 解析 Hive 日志 SQL。
7. 解析任务页面 SQL。
8. 按任务类型策略合并 SQL 事实。
9. 构建初始图。
10. 从图中的表采集 SzConnector 元数据。
11. 抽取字段血缘。
12. 重建图。
13. 对新暴露的图谱表再次采集 SzConnector 元数据。
14. 如配置了多轮字段血缘解析，则重复字段血缘 pass。
15. 构建最终图。
16. 审计图谱事实。
17. 导出 Neo4j schema、导入 Cypher 和查询模板。
18. 执行离线图查询校验。
19. 可选构建 LLM 证据、代码口径、口径比对和增强图。
20. 可选审计、导出、校验 LLM 增强图。
21. 可选导入并校验 Neo4j；如果启用 `--build-llm`，导入前缀为 `strategy_llm`。
22. 生成最终质量报告。

## 断点复用行为

流水线支持重复运行：

- 血缘批采集会跳过 lineage batch 目录中已经存在的根任务。
- 任务详情默认复用 `task_details.json`，除非使用 `--force-details`。
- 页面代码默认复用 `code_artifacts_page.json`，除非使用 `--force-page-code`。
- Hive 日志默认复用 `log_artifacts_full.json`，除非使用 `--force-logs`。
- SzConnector 元数据会跳过 `sz_metadata/dataset_dms.json` 和 `indicator_registry.json` 中已经存在的记录。

## 强制重采参数

- `--force-details`：重新采集任务详情。
- `--force-page-code`：重新采集任务页面 SQL/配置。
- `--force-logs`：重新采集 Hive 运行日志。

大项目除非必要，不建议随意打开强制重采。

## LLM 参数

- `--build-llm`：构建可选指标口径层。
- `--llm-provider mock`：本地 mock，不调用模型。
- `--llm-provider openai-compatible`：调用 `/chat/completions`，使用 `OPENAI_API_KEY` 或 `LLM_API_KEY`。
- `--llm-model`：覆盖 `LLM_MODEL`。
- `--llm-output-prefix`：覆盖增强图前缀，默认 `strategy_llm`。
- `--llm-max-sql-chars`：限制每条 SQL 进入证据包的最大字符数。
- `--llm-sleep`：真实模型调用时的限速等待。

## Neo4j

只有在本地或目标环境 Neo4j 已启动，并且希望更新数据库时，才使用 `--import-neo4j`。

不使用 `--import-neo4j` 时，最终质量报告仍会生成。如果历史 Neo4j 校验结果和当前 JSONL 图数量不一致，会标记：

```json
"has_stale_neo4j_validation": true
```

多项目导入建议使用独立脚本：

```bash
python3 /Applications/personal-work/kg_probe/import_and_validate_neo4j.py \
  /path/to/project_dir \
  --prefix strategy_llm \
  --project-id sale \
  --replace-project
```

这样只替换指定 `project_id`，不会清空其他项目。

## 项目级假设

- `hiveTask` 和 `hiveTask-2.0` 以运行日志 SQL 为权威代码来源。
- 非 Hive 任务以任务页面 SQL/配置为权威代码来源。
- `sparkIndex` 的目标表/写入逻辑从任务页面 `prepare.sqls` 读取。
- Git 仓库设计态代码采集暂缓。
- 数据服务节点暂缓建模。

## 增量更新集成

`incremental_update.py` 位于完整流水线之前：

1. 从项目登记表读取结果任务和补充任务 ID。
2. 刷新上游血缘、任务详情、页面代码和 Hive 运行 SQL。
3. 如果刷新质量门禁发现缺失或失败产物，则停止。
4. 比较任务元信息、依赖边、原始代码哈希和规范化 SQL 语义哈希。
5. 如果没有语义变化，直接返回。
6. 如果存在变化，计算受影响下游任务和指标。
7. 调用完整项目流水线重建并校验项目。

第一版增量更新刻意选择“项目重建”，而不是逐任务 patch Neo4j。操作文件和命令见 `INCREMENTAL_UPDATE.md`。
