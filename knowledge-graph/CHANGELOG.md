# 变更记录

本文件采用追加式记录：最新变更放在前面，历史阶段和关键优化继续保留，避免只剩当前版本状态。

## 2026-07-21 查询层与多项目导入

### 变更

- `import_and_validate_neo4j.py`
  - 支持 `--project-id` 和 `--replace-project`。
  - 多项目导入时使用 `project_id::原始id` 作为 Neo4j 节点 ID，避免不同项目实体冲突。
  - `--replace-project` 只删除并替换指定项目，不再要求清空整库。
  - 修正 `Project.project_id`，确保项目列表、图谱状态查询和 `project_key` 一致。
  - 增加项目内唯一约束和 `KGNode` 全文索引 `kg_entity_search`。

- `query_layer/service.py`
  - `search_entities` 优先使用 Neo4j fulltext index，失败或无结果时回退到原有模糊搜索。
  - `analyze_impact` 改为先聚合全量受影响字段、表、任务、指标，再返回代表路径。
  - 修复字段影响分析中任务数量低估问题，字段变更可同时统计生产任务、血缘边任务和消费任务。
  - `find_definition_issues` 增加口径状态汇总、筛选总数和分页。
  - `analyze_impact`、`find_definition_issues` 支持 `cursor` 分页。

- `query_api.py`
  - 新增轻量 HTTP 查询接口，封装 12 个查询原语。
  - 提供实体搜索、影响分析、口径差异、指标/任务/表/字段上下文、上下游血缘和路径解释接口。

- `QUERY_API_USAGE.md`
  - 新增接口调用说明，列出当前支持项目和常用接口示例。

### 验证

- `digital_operations`、`project_customer_report`、`project_stastic_month`、`t0`、`project_sale_new`、`sale` 已成功导入同一个 Neo4j。
- 查询层真实 Neo4j 冒烟通过，`error_count = 0`。
- `digital_operations` 中 `running_phase` 精确字段影响分析结果：
  - 受影响字段：`20`
  - 受影响表：`21`
  - 受影响任务：`32`
  - 受影响指标：`0`

## 历史里程碑

### 项目立项与双层图方案

- 明确图谱采用“双层图”：
  - 底层：代码语义图，包括任务、SQL、表、字段、字段血缘、运行日志。
  - 上层：业务/数据资产图，包括指标、指标登记口径、代码口径、数据层级、负责人。
  - 中间通过 `READS`、`WRITES`、`PRODUCES`、`CONSUMES`、`DEPENDS_ON`、`STORED_IN`、`COMPUTED_BY`、`OWNS`、`HAS_CODE_DEFINITION` 等边连接。
- 明确券商大数据分层口径：
  - `odata -> pdata -> dm_index_n -> dm -> 数据服务`
  - 数据服务层暂缓建模，先把采集、解析、事实和图谱层做扎实。
- 明确代码优先原则：
  - 指标口径以代码和表元信息为主。
  - 登记口径作为重要参考，但不是最终裁决来源。

### 采集层建设

- 建立从结果任务 ID 反向穿透上游调度链路的采集流程。
- 支持多根任务 ID 合并成项目级血缘。
- 采集 Horae 任务详情、上下游依赖、任务页面 SQL/配置、运行日志。
- 明确不同任务类型的代码来源策略：
  - `hiveTask`、`hiveTask-2.0`：从运行日志解析 SQL。
  - `sparkIndex` 等非 Hive 任务：优先从任务页面获取 SQL/配置。
  - 获取不到页面代码时，再考虑运行日志兜底。
- 支持 SzConnector 表元数据、表字段、指标登记信息采集。
- 暂缓 Git repo 设计态代码采集。

### SQL 解析与事实层建设

- 建立 SQL 解析链路：
  - 清洗运行日志噪声。
  - 使用 `sqlglot` 解析 SQL。
  - 对异常 SQL 片段使用保守兜底。
- 输出标准化 SQL 事实：
  - `SqlStatement`
  - `READS`
  - `WRITES`
  - 表级依赖
  - 字段级血缘
- 构建图谱 JSONL：
  - `strategy_graph_nodes.jsonl`
  - `strategy_graph_edges.jsonl`
- 增加事实审计：
  - 缺失端点检查。
  - 必填属性检查。
  - 孤立节点检查。
  - 置信度分布。
  - 指标/表覆盖情况。

### Neo4j 图谱层

- 本地安装并验证 Neo4j。
- 增加 Neo4j schema、约束、索引和导入脚本。
- 支持离线图查询校验和真实 Neo4j 导入校验。
- 确认首个项目图谱可以支持：
  - 调度依赖查询。
  - 表级血缘查询。
  - 字段影响分析。
  - 指标上下文查询。

### LLM 指标口径层

- 设计并实现 LLM 口径生成流程：
  - 为每个指标组织 SQL、表、字段、任务、登记口径证据包。
  - 生成代码优先指标口径。
  - 将代码口径与登记口径比对。
  - 保存模型名称、证据哈希、Prompt 模板版本和结构化输出。
- 明确 Prompt 模板统一维护在 `llm_prompt_templates.json`，不需要每个指标重复保存完整模板正文。
- 支持 OpenAI 兼容模型接口，已验证 DeepSeek 调用链路。
- 将 LLM 口径事实合并进增强图：
  - `EvidenceBundle`
  - `PromptTemplate`
  - `PromptRun`
  - `ModelVersion`
  - `CodeDefinition`
  - `DefinitionComparison`

### 增量更新设计

- 建立轻量项目登记表方案：
  - 用户维护项目根任务 ID 和补充任务 ID。
  - 系统定期扫描对应任务、依赖和 SQL 是否变化。
- 第一版增量更新采用“发现语义变化后重建项目”，暂不做逐任务 Neo4j patch。
- 对比维度包括：
  - 任务元信息。
  - 调度依赖。
  - 原始代码哈希。
  - 规范化 SQL 语义哈希。
- 增量扫描产物包括：
  - `incremental/current_snapshot.json`
  - `incremental/state.json`

### 查询层设计

- 设计标准返回协议：
  - `status`
  - `answer`
  - `data`
  - `entities`
  - `paths`
  - `evidence`
  - `warnings`
  - `graph_context`
  - `page`
- 设计并实现 12 个查询原语：
  - `search_entities`
  - `resolve_entity`
  - `get_metric_context`
  - `get_task_context`
  - `get_dataset_context`
  - `get_column_context`
  - `trace_upstream`
  - `trace_downstream`
  - `analyze_impact`
  - `compare_metric_definitions`
  - `find_definition_issues`
  - `explain_lineage_path`
- 将查询层定位为后续人、智能体和大模型共同使用的能力层，而不是只服务一个前端页面。

### 前端探索与收敛

- 曾实现过轻量看板和问答页面原型，用于展示项目资产概览、局部图谱和问答入口。
- 后续评估认为前端方案还不够贴合最终形态，决定先删除 dashboard 前端代码。
- 当前保留 HTTP 查询接口和原子查询能力，前端或机器人后续基于接口重新设计。

## 2026-07-14 字段血缘生成表达式

### 变更

- `extract_column_lineage.py`
  - 将字面量和生成表达式投影从错误重新分类为可解释字段事实。
  - 为生成字段增加 `generation_type`：
    - `literal`：固定值，例如 `'RCC' AS data_src_cd` 或 `'' AS remark`。
    - `generated_expression`：不含源字段的表达式，例如 `from_unixtime(unix_timestamp()) AS data_time`。
  - 将未解析源字段错误重命名为 `projection_without_resolved_source_column`。
  - 增加本地 SQL 路径兜底，支持 Windows 内网采集产物拷回 macOS 后重新解析。
  - 扩展 Hive 日志噪声过滤规则，覆盖常见执行输出片段。

- `build_graph_facts.py`
  - 新增 `GeneratedExpression` 节点。
  - 新增 `Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression` 边。
  - 保持真实源字段血缘为 `Column -[:DERIVED_FROM]-> Column`。

- `report_project.py`
  - 增加生成字段数量和生成类型分布。
  - 增加 `explainable_column_fact_count` 和 `explainable_column_fact_pct`，同时覆盖已解析源字段和生成字段。

- `audit_graph_facts.py`
  - 增加 `generated_by_expression_count`。

### 在 `new_project` 上的验证

变更前：

- 字段血缘事实：`54035`
- 字段血缘错误：`19993`
- `projection_without_source_column`：`16225`
- 图节点：`90191`
- 图边：`151233`

变更后：

- 字段血缘事实：`70260`
- 源字段血缘事实：`23510`
- 生成字段事实：`16225`
  - `literal`：`15297`
  - `generated_expression`：`928`
- 字段血缘错误：`3768`
- `projection_without_source_column` 已从错误分布中移除。
- 图节点：`117802`
- 图边：`178844`
- `GeneratedExpression` 节点：`16225`
- `GENERATED_BY_EXPRESSION` 边：`16225`
- 图审计：无缺失端点，无必填属性缺失。

## 2026-07-14 Hive 到外部库同步血缘

### 变更

- `build_graph_facts.py`
  - 对 `hive2*` 任务，不再把任务名或任务描述当作产出表。
  - 使用 `sync_info["Hive源库"] + sync_info["Hive源表"]` 作为消费的 Hive 表。
  - 使用 `sync_info["目标库表"]` 作为产出的外部表。

- `extract_column_lineage.py`
  - 对 `hive2*` 任务，将选中的 Hive 字段映射到外部目标表字段。
  - 为该映射方式增加 `target_resolution = "task_sync_target"`。
  - 源表保持为 Hive 表，目标表保持为非 Hive 表，例如 PostgreSQL 或 Oracle 目标表。

### 在 `new_project` 上的验证

- `ambiguous_task_outputs` 从 `9` 降为 `0`。
- `task_sync_target` 字段事实：`412`。
- 示例：
  - `dm_om_n.wt_cust_emp_dev_rela_info.cust_id`
  - `-> aumcrmii.mv_khxx_khgx.cust_id`
  - 经由任务 `207818`。
- 已移除检查过的同步任务中由任务名误生成的假产出表：
  - `hive2pg.wt_cust_emp_dev_rela_info`
  - `crmii.erp_a_gf_emp_info_v_new`
  - `src_gfjgj.erp_a_gf_*_kxc`

## 2026-07-14 CTAS、UNION 和星号展开

### 变更

- `extract_column_lineage.py`
  - 增加 `CREATE TABLE ... AS SELECT ...` 兜底解析，覆盖 sqlglot 将其分类为 `Command` 的情况。
  - 增加 `UNION` / `UNION ALL` 分支感知投影抽取。
  - 增加 CTAS 目标表识别，CTAS 字段血缘现在指向创建表，而不是回退到任务级产出。
  - 增加 CTAS 推断 schema，支持后续语句展开 `TEMP.xxx B` 中的 `B.*`。
  - 增加简单子查询别名映射，支持 `A.*` 等情况。
  - 增加未限定表引用的唯一表名后缀 schema 匹配。
  - 规范化字段前清理 DMS 字段名中的 HTML 标签。

- `build_graph_facts.py`
  - 将 `branch_ordinal` 写入生成边 ID 和边属性，避免 `UNION` 分支互相覆盖。

### 在 `new_project` 上的验证

变更前，在生成表达式和 hive2 同步修复之后：

- 字段血缘事实：`70392`
- 源字段血缘事实：`23619`
- 生成字段事实：`16225`
- 错误：`3763`
- `schema_star_expand`：`1043`

变更后：

- 字段血缘事实：`84175`
- 源字段血缘事实：`51193`
- 生成字段事实：`17566`
- 错误：`3242`
- `schema_star_expand`：`8239`
- `ctas_target` 事实：`28279`
- 推断 CTAS schema：`645`
- 图节点：`137369`
- 图边：`226877`
- `DERIVED_FROM` 边：`51193`
- `GENERATED_BY_EXPRESSION` 边：`17566`
- 图审计：无缺失端点，无必填属性缺失。

### 已解决示例

- `100514_b24c0c48a5c87d92`
  - 原先：`no_select_projection`
  - 现在：跨 CTAS/UNION 分支生成 `54` 条事实，`0` 个错误。

- `100514_a045e42c69661101`
  - 原先：`A.*` 和 `B.*` 报 `projection_without_output_name`
  - 现在：生成 `31` 条事实，`0` 个错误。
  - `A.*` 从 `pdata_n.t01_pty_stati_info_h` 展开。
  - `B.*` 从 `temp.t01_pty_stati_info_h_temp_rcc003` 的推断 schema 展开。
