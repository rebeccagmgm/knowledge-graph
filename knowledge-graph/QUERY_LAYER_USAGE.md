# 查询层使用说明

查询层当前提供Python内核和CLI，REST与MCP后续复用同一服务类。

## 图谱状态

```bash
cd /Applications/personal-work/kg_probe
python3 -m query_layer.cli get_graph_status --pretty
```

## 搜索实体

```bash
python3 -m query_layer.cli search_entities --pretty --json '{
  "query": "打新次数",
  "entity_types": ["metric"]
}'
```

## 指标上下文

```bash
python3 -m query_layer.cli get_metric_context --pretty --json '{
  "metric_id": "ind2023030962774756"
}'
```

## 上游追踪

```bash
python3 -m query_layer.cli trace_upstream --pretty --json '{
  "subject": {"entity_type": "schedule_task", "key": "152285"},
  "max_hops": 20,
  "limit": 20
}'
```

## 字段影响分析

```bash
python3 -m query_layer.cli analyze_impact --pretty --json '{
  "subject": {
    "entity_type": "column",
    "entity_id": "column:dm_xxx.table_a.running_phase"
  },
  "change_type": "drop",
  "max_hops": 20,
  "include_sql_fallback": true
}'
```

## 路径解释

```bash
python3 -m query_layer.cli explain_lineage_path --pretty --json '{
  "from_entity_id": "dataset:dm_a.table_a",
  "to_entity_id": "dataset:pdata_a.table_b",
  "max_hops": 20
}'
```

## 已确认结果分支比较

这里的“结果分支”是以一个已确认结果任务为入口、在指定深度内展开的关联图谱范围，
不是 Git 分支。`compare_branches` 只比较调用方明确传入的结果分支，不发现或扩展业务范围。
它在保留任务、数据集共同/差异/独有实体的同时，支持指标、指标定义、SQL 节点和结果字段
分层 EntitySet，并返回局部共享组、两两相似度和有图谱证据的结果字段派生差异。
`max_depth` 同时控制上游任务和数据集依赖深度；实际使用应显式选择深度，避免深层通用依赖
掩盖结果附近的结构差异。

服务会先按完整请求范围执行。仅当 Neo4j 返回事务内存上限错误时，才自动二分物理查询
批次并合并原始结果；请求的结果分支范围和 `max_depth` 不会改变。恢复成功时响应保持
`ok`，并返回 `QUERY_RECOVERED_BY_BATCH_SPLIT` 信息提示及 `data.execution` 执行元数据。
超过十个结果分支的请求会使用最多十支的物理批次，但最终共同、差异、局部共享和两两
比较仍在完整请求范围上统一计算。

先调用 `get_graph_native_capabilities` 取得当前构建版本和已实现操作。当前只声明
`compare_branches`；其余 graph-native 操作不会因为出现在其他契约中就被误报为可用。

```bash
python3 -m query_layer.cli compare_branches --pretty --json '{
  "project_id": "project_sale_new",
  "contract_version": "graph-native:v1",
  "graph_build_id": "BUILD_ID",
  "confirmed_branch_ids": ["task:158050", "task:220979", "task:114013"],
  "max_depth": 2,
  "limit": 100
}'
```

`partially_shared_entity_groups` 按精确分支成员组合分组。每个已返回的共享组包含一个
`structural_summary`，只汇总当前图中实际存在、且两端实体都属于该精确共享组的完整关系
实例。它表达的是精确成员组的诱导子图，不证明这些关系实际参与了每个成员分支的遍历路径。
摘要返回观测连通组件数、连通及未连通共享实体数、关系数量与类型，以及
最多三个稳定排序的后续查询入口。连通计算使用关系的无向投影；它不是图同构，也不是
完整业务子图，跨成员组合的关系不会计入。

结果中的单一 `topology_scope` 声明 `max_depth`、查询支持的关系类型和
`LITERAL_RELATIONSHIP_INSTANCES_IN_EXACT_MEMBERSHIP_INDUCED_SUBGRAPH` 语义。所有结构字段均使用 `observed`
限定；零条观测关系不证明业务上没有结构。若有请求分支未解析，或拓扑查询未完成，服务
保留原有比较字段并返回 `partial`、`STRUCTURAL_TOPOLOGY_UNAVAILABLE`，且不生成容易被误解
为零观测的 `structural_summary`。`common_entity_ids` 继续保留，但不会被解释为共同业务骨架。

若图中没有写入方式属性，`write_mode_by_branch` 明确返回 `UNKNOWN`，不会从任务名或表名
推断。当前图没有 Expression 实体层时，`component_counts.expressions` 返回 `null`，并在
`evidence_status/evidence_gaps` 中标记 `UNSUPPORTED`，不会用 0 表示“确认没有表达式”。
未关联到指标、SQL 或结果字段也会标记为证据缺口，不能据此推断业务上不存在对应内容。

请求也可通过`--file request.json`或标准输入传入。

## Python调用

```python
from query_layer import QueryService
from query_layer.neo4j_store import Neo4jStore

store = Neo4jStore.from_password_file(
    "bolt://localhost:7687",
    "neo4j",
    "/secure/neo4j_password.txt",
)
service = QueryService(store, "trial_project", "/data/projects/trial_project")
result = service.execute("search_entities", {"query": "打新次数", "entity_types": ["metric"]})
store.close()
```

所有调用都返回`QUERY_LAYER_DESIGN.md`规定的统一响应信封。
