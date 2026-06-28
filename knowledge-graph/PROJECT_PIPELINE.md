# 项目级流水线

`run_project_pipeline.py`是多结果任务项目的统一入口。

## 基本用法

直接传入任务ID：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --tasks 236334,212769,207174
```

任务文件支持逗号分隔或每行一个ID：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file examples/task_ids.example.txt
```

预演模式只输出将执行的命令，不访问内部服务：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --tasks 236334,212769,207174 \
  --dry-run
```

启用LLM指标口径层：

```bash
export LLM_API_KEY="..."
export LLM_BASE_URL="https://api.deepseek.com"
export LLM_MODEL="deepseek-v4-pro"

python3 kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file examples/task_ids.example.txt \
  --build-llm \
  --llm-provider openai-compatible
```

默认LLM提供方为`mock`，用于在不调用真实模型的情况下验证证据、请求和构图链路。

## 输出目录

建议配置：

```bash
export KG_LINEAGE_ROOT="$PWD/artifacts/lineage_batch"
export KG_OUTPUT_ROOT="$PWD/artifacts/projects"
```

对于`--project-id my_project`，主要输出为：

```text
artifacts/lineage_batch/my_project
artifacts/projects/my_project
```

最终产物包括：

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
- `strategy_neo4j_validation.json`，仅在使用`--import-neo4j`时生成
- `strategy_quality_report.json`
- `llm/evidence_bundles.jsonl`，仅在使用`--build-llm`时生成
- `llm/code_definition_requests.jsonl`
- `llm/code_definitions.jsonl`
- `llm/definition_comparisons.jsonl`
- `strategy_llm_graph_nodes.jsonl`
- `strategy_llm_graph_edges.jsonl`
- `project_pipeline_manifest.json`

## 执行顺序

1. 分别从每个结果任务递归采集上游血缘。
2. 将多个入口的血缘快照合并为项目级调度图。
3. 采集Horae任务详情。
4. 采集任务页面SQL和配置。
5. 仅为`hiveTask`、`hiveTask-2.0`采集运行日志。
6. 解析Hive日志SQL。
7. 解析任务页面SQL。
8. 按任务类型选择权威SQL来源并合并事实。
9. 构建初始图。
10. 根据图中的表采集SzConnector元数据。
11. 提取字段血缘。
12. 重新构图。
13. 为字段解析中新发现的表补采SzConnector元数据。
14. 按配置重复字段血缘提取。
15. 构建最终基础图。
16. 审计图事实。
17. 导出Neo4j Schema、导入Cypher和查询模板。
18. 离线验证图查询。
19. 可选构建LLM证据、代码口径、登记口径比较和增强图。
20. 可选审计并验证LLM增强图。
21. 可选导入Neo4j；启用LLM时导入`strategy_llm`。
22. 生成最终质量报告。

## 断点续跑

- 血缘批处理跳过已存在的入口任务快照。
- `task_details.json`存在时复用任务详情，除非指定`--force-details`。
- `code_artifacts_page.json`存在时复用页面代码，除非指定`--force-page-code`。
- `log_artifacts_full.json`存在时复用Hive日志，除非指定`--force-logs`。
- SzConnector跳过已存在于`dataset_dms.json`和`indicator_registry.json`的对象。
- LLM脚本使用`--resume`时跳过状态为`ok`的结果，并重试失败项。

## 强制刷新参数

- `--force-details`：重新采集任务详情。
- `--force-page-code`：重新采集任务页面SQL和配置。
- `--force-logs`：重新采集Hive运行日志。

大项目仅在确认数据过期时使用强制刷新参数。

## LLM参数

- `--build-llm`：构建指标口径层。
- `--llm-provider mock`：不调用模型，只验证本地流程。
- `--llm-provider openai-compatible`：调用兼容`/chat/completions`的接口。
- `--llm-model`：覆盖`LLM_MODEL`。
- `--llm-output-prefix`：增强图前缀，默认为`strategy_llm`。
- `--llm-max-sql-chars`：限制证据包中每条SQL的长度。
- `--llm-sleep`：控制真实模型请求间隔。

真实模型调用会发送SQL、表元数据和指标登记信息，应先完成数据安全审批。

## Neo4j

只有在Neo4j已启动且确实需要更新数据库时才使用`--import-neo4j`。

未导入Neo4j时，最终质量报告仍会生成。如果旧的Neo4j验证数量与当前JSONL图不一致，报告中会标记：

```json
{"has_stale_neo4j_validation": true}
```

## 当前工程假设

- `hiveTask`和`hiveTask-2.0`以运行日志SQL为准。
- 非Hive任务以任务页面SQL或配置为准。
- `sparkIndex`从任务页面`prepare.sqls`识别目标表。
- Git设计态代码采集暂缓。
- 数据服务节点暂缓。
- 指标登记信息仅作比较依据，冲突时以代码和表元数据优先。

