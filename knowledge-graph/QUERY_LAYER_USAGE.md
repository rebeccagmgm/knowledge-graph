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
