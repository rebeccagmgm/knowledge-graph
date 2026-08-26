# 知识图谱查询接口调用说明

本文档说明如何调用知识图谱查询接口。接口按 `project_id` 隔离查询，不同项目的数据不会混查。

## 1. 当前支持项目

| project_id | 项目名称 |
| --- | --- |
| `digital_operations` | 数字化运营 |
| `project_customer_report` | 客户报告 |
| `project_stastic_month` | 统计月报 |
| `t0` | t0 |
| `project_sale_new` | 交叉销售 |
| `sale` | 交叉销售（全） |

## 2. 调用约定

接口基础地址由部署方提供，下文统一用 `{BASE_URL}` 表示，例如：

```text
http://10.x.x.x:8790
```

所有查询接口均使用 `POST`，请求头：

```text
Content-Type: application/json
```

请求体必须包含：

```json
{
  "project_id": "sale"
}
```

通用分页参数：

```json
{
  "limit": 20,
  "cursor": "上一页返回的 next_cursor，可选"
}
```

通用响应结构：

```json
{
  "request_id": "req_xxx",
  "primitive": "search_entities",
  "status": "ok",
  "answer": "面向人的简要回答",
  "data": {},
  "entities": [],
  "paths": [],
  "evidence": [],
  "warnings": [],
  "graph_context": {},
  "page": {
    "limit": 20,
    "returned": 20,
    "next_cursor": null,
    "has_more": false
  }
}
```

`status` 常见值：

| status | 含义 |
| --- | --- |
| `ok` | 查询成功 |
| `partial` | 查询成功但结果可能不完整，需结合 `warnings` |
| `ambiguous` | 命中多个候选实体，需要进一步消歧 |
| `not_found` | 未找到匹配实体 |
| `error` | 查询失败 |

## 3. 辅助接口

### 3.1 健康检查

接口：

```text
GET {BASE_URL}/health
```

用于确认查询服务和 Neo4j 连接是否可用。

### 3.2 查询已导入项目

接口：

```text
GET {BASE_URL}/api/projects
```

返回已导入 Neo4j 的项目列表，以及每个项目的节点数、边数、任务数和指标数。

### 3.3 查询项目图谱状态

接口：

```text
GET {BASE_URL}/api/projects/{project_id}/graph-status
```

用于查看某个项目的图谱前缀、构建批次、构建时间和增量扫描状态。

### 3.4 查询接口原语映射

接口：

```text
GET {BASE_URL}/api/primitives
```

用于查看 HTTP 友好路径和底层查询原语的映射关系。

## 4. 搜索实体

用于搜索指标、任务、表、字段。

接口：

```text
POST {BASE_URL}/api/entities/search
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/entities/search \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "query": "理财产品购买金额",
    "entity_types": ["metric", "dataset", "column"],
    "limit": 10
  }'
```

`entity_types` 可选：

```text
metric, schedule_task, dataset, column, sql_statement
```

## 5. 解析实体

用于将用户输入的关键词、任务 ID、表名、字段名、指标 ID 解析为图谱中的确定实体。机器人在执行上下文查询、血缘查询、影响分析前，可以先调用该接口做实体消歧。

接口：

```text
POST {BASE_URL}/api/query/resolve_entity
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/query/resolve_entity \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "query": "241721",
    "entity_type": "schedule_task",
    "limit": 5
  }'
```

也可以解析表或字段：

```json
{
  "project_id": "digital_operations",
  "query": "running_phase",
  "entity_type": "column",
  "limit": 5
}
```

如果返回 `status=ambiguous`，说明存在多个候选实体，需要用户补充表名、任务 ID 或指标 ID。

## 6. 影响分析

用于分析字段、表、任务变更会影响哪些下游字段、表、任务、指标。

接口：

```text
POST {BASE_URL}/api/impact/analyze
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/impact/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "digital_operations",
    "entity_id": "column:temp_n.wt_ioc_lifecycle_strategy_pty_uniq.running_phase",
    "change_type": "drop",
    "limit": 20
  }'
```

`change_type` 可选：

```text
drop, rename, type_change, logic_change, stop_production, schedule_change
```

返回重点：

| 字段 | 含义 |
| --- | --- |
| `data.summary.affected_column_count` | 受影响字段总数 |
| `data.summary.affected_dataset_count` | 受影响表总数 |
| `data.summary.affected_task_count` | 受影响任务总数 |
| `data.summary.affected_metric_count` | 受影响指标总数 |
| `data.affected_columns` | 受影响字段明细 |
| `data.affected_datasets` | 受影响表明细 |
| `data.affected_tasks` | 受影响任务明细 |
| `data.affected_metrics` | 受影响指标明细 |
| `paths` | 代表性血缘路径 |
| `evidence` | 支撑证据 |

## 7. 查询口径差异

用于查询登记口径与代码口径、LLM 理解口径不一致的指标。

接口：

```text
POST {BASE_URL}/api/definitions/issues
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/definitions/issues \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "issue_types": ["conflict", "partially_consistent"],
    "limit": 10
  }'
```

`issue_types` 可选：

| issue_type | 含义 |
| --- | --- |
| `conflict` | 登记口径与代码口径存在冲突 |
| `partially_consistent` | 部分一致，但存在缺失或差异 |
| `code_evidence_insufficient` | 代码证据不足，不能给出确定结论 |
| `registry_missing` | 缺少登记口径 |
| `consistent` | 登记口径与代码口径一致 |

返回重点：

| 字段 | 含义 |
| --- | --- |
| `data.summary` | 各类口径状态数量 |
| `data.selected_total` | 当前筛选条件下的问题总数 |
| `data.issues` | 问题指标明细 |
| `data.issues[].registered_definitions` | 登记口径 |
| `data.issues[].code_summary` | LLM 基于代码理解的口径摘要 |
| `data.issues[].conflict_points` | 冲突点 |
| `data.issues[].recommended_definition` | 建议口径 |

## 8. 比对单个指标口径

用于查询某一个指标的登记口径、代码口径和 LLM 比对结论。它更适合回答“这个指标的登记口径和代码口径是否一致”。

接口：

```text
POST {BASE_URL}/api/metrics/definition-compare
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/metrics/definition-compare \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "query": "理财产品购买金额",
    "limit": 5
  }'
```

也可以直接按指标 ID 查询：

```json
{
  "project_id": "sale",
  "metric_id": "indxxxxxxxx"
}
```

返回重点：

| 字段 | 含义 |
| --- | --- |
| `data.comparison_status` | 口径比对状态 |
| `data.code_definitions` | LLM 基于代码理解的口径 |
| `data.registered_definitions` | 登记口径 |
| `data.comparisons` | 差异点、缺失点和建议口径 |
| `evidence` | 支撑代码、SQL、表、任务证据 |

## 9. 查询指标上下文

用于查询某个指标的登记口径、代码口径、存储表、计算任务和证据。

接口：

```text
POST {BASE_URL}/api/metrics/context
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/metrics/context \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "t0",
    "query": "理财产品购买金额",
    "limit": 5
  }'
```

也可以直接按指标 ID 查询：

```json
{
  "project_id": "t0",
  "metric_id": "indxxxxxxxx"
}
```

## 10. 查询任务上下文

用于查询任务元信息、读写表、上下游任务、SQL 证据。

接口：

```text
POST {BASE_URL}/api/tasks/context
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/tasks/context \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "task_id": "241721"
  }'
```

## 11. 查询表上下文

用于查询表的层级、字段、上下游表、生产任务、消费任务。

接口：

```text
POST {BASE_URL}/api/datasets/context
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/datasets/context \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "query": "dm_index_n.dm_co_t0_trd_prnt_stmt_perf"
  }'
```

## 12. 查询字段上下文

用于查询字段所属表、字段血缘、影响路径和证据。

接口：

```text
POST {BASE_URL}/api/columns/context
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/columns/context \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "digital_operations",
    "query": "running_phase",
    "limit": 5
  }'
```

## 13. 查询上游血缘

接口：

```text
POST {BASE_URL}/api/lineage/upstream
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/lineage/upstream \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "subject": {
      "entity_type": "schedule_task",
      "key": "241721"
    },
    "max_hops": 10,
    "limit": 20
  }'
```

## 14. 查询下游血缘

接口：

```text
POST {BASE_URL}/api/lineage/downstream
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/lineage/downstream \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "query": "dm_index_n.dm_co_t0_trd_prnt_stmt_perf",
    "entity_type": "dataset",
    "max_hops": 10,
    "limit": 20
  }'
```

## 15. 解释两点之间的血缘路径

用于解释两个实体之间是否存在可解释路径。

接口：

```text
POST {BASE_URL}/api/path/explain
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/path/explain \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "from_entity_id": "task:241721",
    "to_entity_id": "dataset:dm_index_n.dm_co_t0_trd_prnt_stmt_perf",
    "max_hops": 10
  }'
```

## 16. 查询增量变化事件

用于查询项目最近的增量扫描结果，包括任务新增删除、代码语义变化、依赖变化、表结构变化、指标登记变化和受影响任务/指标摘要。

接口：

```text
POST {BASE_URL}/api/changes/recent
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/changes/recent \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "sale",
    "limit": 10
  }'
```

返回重点：

| 字段 | 含义 |
| --- | --- |
| `data.summary.event_count` | 已记录的增量变化事件数 |
| `data.summary.semantic_change_event_count` | 有语义变化的事件数 |
| `data.events[].code_changed_task_count` | 代码语义变化任务数 |
| `data.events[].affected_task_count` | 受影响任务数 |
| `data.events[].affected_metric_count` | 受影响指标数 |

## 17. 展开局部知识图谱

用于从任务、表、字段、指标等任意实体出发，按方向、深度和关系范围返回一张可直接画图的局部子图。适合汇报展示、智能体取证、大模型上下文压缩。

接口：

```text
POST {BASE_URL}/api/graph/neighborhood
```

请求示例：

```bash
curl -s -X POST {BASE_URL}/api/graph/neighborhood \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "iresearch",
    "subject": {
      "key": "upper_grp_name",
      "entity_type": "column"
    },
    "direction": "downstream",
    "relation_profile": "column_lineage",
    "max_hops": 3,
    "limit_nodes": 100,
    "limit_edges": 300
  }'
```

常用参数：

| 字段 | 含义 |
| --- | --- |
| `subject.entity_id` | 精确实体 ID，优先推荐 |
| `subject.key` + `subject.entity_type` | 模糊锚点，会触发实体解析和消歧 |
| `direction` | `upstream`、`downstream`、`both` |
| `relation_profile` | `schedule`、`dataset_lineage`、`column_lineage`、`code`、`metric`、`lineage`、`all_safe` |
| `max_hops` | 展开深度，展示用建议 2-4 跳 |
| `limit_nodes` / `limit_edges` | 控制局部图大小，防止大图爆炸 |

返回重点：

| 字段 | 含义 |
| --- | --- |
| `data.visual_graph.nodes` | 页面可直接渲染的节点 |
| `data.visual_graph.edges` | 页面可直接渲染的边，`source/target` 按展开方向排列 |
| `data.summary.truncated` | 是否发生截断 |
| `entities` / `evidence` / `paths` | 兼容标准查询协议的实体、证据和样例路径 |

汇报展示页：

```text
GET {BASE_URL}/showcase
```

该页面会读取 `/api/projects` 和 `/api/graph/neighborhood`，展示已导入项目概览，并支持从锚点展开局部知识图谱。

如果直接双击 HTML 文件，以 `file://` 方式打开页面，页面左侧的“接口地址”会默认使用 `http://127.0.0.1:8790`。如果查询服务运行在其他机器或端口，请在该输入框中改成实际地址。

## 18. 分页查询

如果响应中：

```json
{
  "page": {
    "next_cursor": "eyJvZmZzZXQiOjEwfQ",
    "has_more": true
  }
}
```

说明还有下一页。继续查询时将 `next_cursor` 原样放入请求体：

```json
{
  "project_id": "sale",
  "query": "dm_index_n",
  "entity_types": ["dataset"],
  "limit": 10,
  "cursor": "eyJvZmZzZXQiOjEwfQ"
}
```

## 19. 调用建议

- 业务口径答疑优先使用：`/api/entities/search`、`/api/metrics/context`、`/api/definitions/issues`。
- 取数逻辑答疑优先使用：`/api/datasets/context`、`/api/tasks/context`、`/api/lineage/upstream`。
- 下游影响分析优先使用：`/api/impact/analyze`。
- 项目变更回顾优先使用：`/api/changes/recent`。
- 汇报展示和局部证据图优先使用：`/api/graph/neighborhood` 或 `/showcase`。
- 如果搜索结果 `status=ambiguous`，应让用户补充表名、字段名、任务 ID 或指标 ID 后再查询。
