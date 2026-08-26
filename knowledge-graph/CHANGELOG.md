# 变更记录

本文件采用追加式记录：最新变更放在前面，历史阶段和关键优化继续保留，避免只剩当前版本状态。

## 2026-08-21 增量更新验证与扫描器优化

### 背景

恢复本地 `horae-cli` 和 `szconnector-cli` 鉴权后，对增量更新链路做了一次真实项目验证。数字化运营项目的首轮强制扫描能跑通，但暴露出运行日志日期噪声和血缘刷新成本偏高的问题。

### 变更

- `incremental_update.py`
  - 对 `runtime_log` 来源 SQL 增加运行日期/时间语义归一化，屏蔽 `BUSI_DATE`、`DATA_ETL_DATE`、`DATA_UPT_DATE`、`DATA_TIME`、`LOAD_TIME` 等典型运行时字面量，降低每日调度日志导致的伪 `code_changed`。
  - 增加 `step_started` / `step_finished` 进度事件，输出各刷新阶段耗时、返回码和 stdout/stderr 尾部，便于长任务期间判断卡点。
  - 增加 `options.force_lineage_refresh` 配置；默认保持强制刷新，也支持复用已有 root lineage 快照。

- `collect_lineage_batch.py`
  - 无待采集任务时不再提前初始化 Horae API，缓存命中时直接输出 `cached` 状态。
  - manifest 同时记录 `requested_task_count` 和实际 `task_count`，避免缓存子集刷新时误解覆盖范围。
  - 批内共享直接上游查询缓存，减少多个入口任务重叠上游时的重复 Horae 调用。

- 文档与配置
  - 更新 `INCREMENTAL_UPDATE.md`、`project_registry.example.json` 和当前 registry 示例配置，记录血缘复用开关和进度事件。

### 验证

- 工具连通性：
  - `szconnector-cli dms t02_scr_base_info -s 10` 可返回 `pdata_news_n.t02_scr_base_info@gfhive`。
  - `horae-cli detail 192810`、`horae-cli search t02_scr_base_info --status` 可正常返回任务信息。
- 数字化运营项目 `trial_project` 实际强制扫描：
  - 产物：`kg-code-snapshots/projects/trial_project/incremental/changes/2026-08-21T174532_0800.json`
  - 状态：`changed`
  - 影响任务：`1482`
  - 影响指标：`309`
  - 任务新增/删除：`19` / `21`
  - 元数据变更：`54`
  - 代码语义变更：`1097`
  - 依赖边新增/删除：`74` / `83`
  - 刷新质量检查通过：无缺 root、详情、页面代码和日志错误。
- 噪声分析：
  - 大量 `code_changed` 来自运行日志日期滚动和旧快照 SQL 抽取污染；例如部分旧 SQL 文件混入 Kyuubi 日志文本，且运行日期从 `2026-06-24` 滚动到 `2026-08-20`。
- 优化后验证：
  - 重新初始化 `trial_project` 基线后，离线增量扫描为 `unchanged`，受影响任务和指标均为 `0`。
  - 单元测试 `test_incremental_update.py`：`11 tests OK`。

### 追加验证：统计月报项目

- 从历史归档快照恢复 `project_stastic_month` 到当前快照区，并用 10 个入口任务建立临时 registry：
  - `200048`
  - `198739`
  - `199727`
  - `199706`
  - `199482`
  - `199408`
  - `200633`
  - `202124`
  - `200257`
  - `201718`
- 初始化增量基线：
  - 任务数：`267`
  - 快照：`kg-code-snapshots/projects/project_stastic_month/incremental/current_snapshot.json`
- 使用本地 Horae/SzConnector 鉴权跑真实强制刷新，不触发 rebuild：
  - 变更产物：`kg-code-snapshots/projects/project_stastic_month/incremental/changes/2026-08-21T181500_0800.json`
  - 状态：`changed`
  - 影响任务：`146`
  - 影响指标：`10`
  - 任务新增/删除：`4` / `0`
  - 元数据变更：`4`
  - 代码语义变更：`140`
  - 文本级非语义变更：`0`
  - 依赖边新增/删除：`5` / `0`
  - 数据集 schema 变更：`false`
  - 指标登记变更：`false`
- 刷新质量检查通过：
  - `missing_root_count=0`
  - `lineage_error_count=0`
  - `missing_detail_count=0`
  - `detail_error_count=0`
  - `page_code_error_count=0`
  - `log_error_count=0`
  - `missing_hive_log_count=0`
- 阶段耗时：
  - `refresh_lineage`：`45.874s`
  - `merge_lineage`：`0.072s`
  - `refresh_details`：`105.061s`
  - `refresh_page_code`：`83.914s`
  - `refresh_hive_logs`：`22.294s`
  - `parse_hive_sql`：`2.681s`
  - `parse_page_sql`：`0.302s`
  - `merge_sql_strategy`：`0.041s`

## 2026-08-14 图谱准确性修复：sparkIndex 隐式目标与元数据清洗

### 背景

发现 bad case：`dm_ecom_n.wt_ioc_lifecycle_strategy_uniq.running_phase` 在图谱中无法解释来源。

根因包括：

- sparkIndex 任务页面中 `prepare.sqls` 和 `query.sql` 被当作独立 SQL 解析，未把 `prepare.sqls` 的建表目标作为 `query.sql` 的隐式写入目标。
- 纯 `CREATE TABLE IF NOT EXISTS` 被误解析为表级 READS。
- SzConnector/DMS 返回的字段名、指标名中可能带 `<font color='red'>...</font>` 搜索高亮标签，入图前未统一清洗。
- 字段血缘未传播派生子查询和部分 CTE/UNION 分支，导致外层同名字段解析为 `unknown.running_phase`。

### 变更

- 更新 `extract_sql_facts.py`
  - 页面 SQL 开头允许 SQL 注释，不再因 `-- comment` 跳过真实 `query.sql`。
  - 纯 `CREATE TABLE` 作为 schema DDL 处理，不再生成 READS/WRITES 边。
  - 对 task page 的 `query.sql`，当没有显式写入目标时，从同 task 的 `prepare.sqls` 推断隐式写入表。
  - statement 元数据保留 `prop_name`、`artifact_id`、`table_name_hint` 和 `implicit_write_datasets`。

- 更新 `extract_column_lineage.py`
  - 增强 CTE、派生子查询和 UNION 分支字段来源传播。
  - 对无表名前缀字段，优先使用当前 SELECT 作用域中的单一物理表或单一派生关系解析。
  - 收敛 CTE 置信标签，避免出现过长的 `cte_cte_cte...` 标签。

- 更新 `build_graph_facts.py`
  - 入图前清洗 SzConnector/DMS 数据集、字段、指标、口径和负责人文本中的 HTML 标签。
  - 字段 ID 使用清洗后的规范字段名构造。

### 验证

在 `tmp_trial_project_current_parser` 上使用隔离前缀 `strategy_fix` 验证：

- `152285_query.sql` 正确生成：
  - `WRITES dm_ecom_n.wt_ioc_lifecycle_strategy_uniq`
  - `READS` 13 张上游表。
- `152285_pre.sql` 被识别为 `sqlglot_schema_ddl`，不再生成伪 READS。
- `dm_ecom_n.wt_ioc_lifecycle_strategy_uniq.running_phase` 生成干净 Column 节点。
- 字段血缘新增两条明确来源：
  - `dm_index_n.grp_def.grp_val`
  - `pdata_n.t07_cam_strg.cam_strg_id`
- 隔离版图谱中 `<font` 残留：
  - nodes：0
  - edges：0

### Neo4j 导入

- 基于 `strategy_fix` 合并已有 LLM 口径与口径比对事实，生成 `strategy_fix_llm`。
- 使用 `project_id=digital_operations`、`--replace-project` 重新导入 Neo4j，只替换该项目子图。
- 导入结果：
  - 删除旧项目节点：65,457
  - 导入节点：67,055
  - 导入关系：291,345
  - 缺失端点：0
- 查询层验证：
  - `get_column_context` 可查到 `dm_ecom_n.wt_ioc_lifecycle_strategy_uniq.running_phase` 的来源字段：
    - `dm_index_n.grp_def.grp_val`
    - `pdata_n.t07_cam_strg.cam_strg_id`
  - `find_definition_issues` 可正常查询 LLM 口径比对结果，当前汇总为：`conflict=30`、`code_evidence_insufficient=97`、`partially_consistent=137`、`registry_missing=41`、`consistent=14`。

### 追加修复：prepare.sqls 忽略 ALTER 维护语句

- `prepare.sqls` 中的 `ALTER TABLE ... DROP PARTITION` 属于运行前分区清理/维护动作，不再作为写入目标或隐式目标候选。
- `ALTER` 语句解析为 `sqlglot_maintenance_ddl`，不产生 READS/WRITES 边。
- `ambiguous_prepare_targets` 从 21 个降为 6 个，剩余任务为：
  - `109846`
  - `109849`
  - `134583`
  - `148241`
  - `148401`
  - `148461`
- 典型验证：任务 `196593` 中的 `ALTER TABLE dm_index_n.cust_shence_event_log_secu DROP PARTITION` 已不再入图，`query.sql` 正确通过 prepare 目标绑定到 `dm_index_n.shence_event_log_pv_and_click`。
- 已重新生成并导入 `strategy_fix_llm`：
  - 节点：82,494
  - 关系：308,331
  - 缺失端点：0

### 追加修复：局部图谱查询防路径爆炸

- 修复 bad case：`digital_operations` 项目中以字段 `running_phase` 为锚点，选择“双向 + 血缘综合 + 6 跳”后局部图查询卡住。
- 根因：原实现使用 Neo4j 可变长度路径枚举，字段节点在 `HAS_COLUMN`、`DERIVED_FROM`、`INFLUENCED_BY`、表/任务读写关系混合双向扩展时，会产生大量组合路径。
- 调整 `get_graph_neighborhood`：
  - 从“枚举所有多跳路径”改为“按层 BFS 受控展开邻接边”。
  - 保留 `limit_nodes`、`limit_edges` 上限，达到上限时返回 `partial` 和截断提示。
  - 视觉边仍保留关系类型、置信度、任务 ID、SQL statement ID 等证据字段。
- 验证：
  - 同一 bad case 不再卡住。
  - 返回 `59` 个节点、`220` 条关系。
  - 接口耗时约 `2.7s`，因达到边上限返回 `partial`。

## 2026-08-12 下线旧版 ontology 发现路线

### 变更

- 保留第二版 `ontology_v2` 路线作为唯一 ontology 发现实现。
- 删除旧版单字段口径族发现入口：
  - `discover_concept_families.py`
- 删除旧版 ontology 报告：
  - `DIGITAL_OPERATIONS_ONTOLOGY_REPORT.md`
  - `PROJECT_SALE_NEW_ONTOLOGY_REPORT.md`
  - `SALE_ONTOLOGY_REPORT.md`
  - `T0_ONTOLOGY_REPORT.md`
- 删除历史项目目录中的旧版 `ontology/` 产物目录。
- 更新文档：
  - `TECHNICAL_SOLUTION.md` 只保留 `ontology_v2/` 技术路线。
  - `ONTOLOGY_DISCOVERY_PRACTICE.md` 重写为第二版实践摘要。
  - `PROJECT_SALE_NEW_ONTOLOGY_V2_LLM_REPORT.md` 和 `T0_ONTOLOGY_V2_REPORT.md` 去掉旧版报告依赖。

### 说明

第一版历史记录继续保留在本变更记录中，用于说明项目演进；代码入口、报告和产物不再保留，避免后续误用。

## 2026-08-12 Ontology v2 接入 LLM 精炼

### 变更

- 新增 `refine_ontology_concepts_with_llm.py`
  - 对 `ontology_v2/concept_candidates.jsonl` 中的候选概念调用 LLM 精炼。
  - 输入候选概念、成员字段组、表主题、字段样例、血缘证据和候选关系。
  - 输出建议业务概念名、概念类型、业务解释、适用范围、关键字段、强弱证据、拆分建议、合并建议、证据边界和业务复核问题。
  - 支持 `mock` 和 `openai-compatible` provider。
  - 支持 `LLM_API_KEY`、`OPENAI_API_KEY`、`codex_ds_API_KEY`、`CODEX_DS_API_KEY`。

- 更新 `run_ontology_v2.py`
  - 新增可选参数 `--refine-ontology-llm`。
  - 默认仍为离线规则流程，只有显式启用时才调用 LLM。

- 新增可选入图事实：
  - `OntologyLLMRefinement` 节点。
  - `REFINED_BY_LLM` 关系。

### 验证

- 编译通过：
  - `refine_ontology_concepts_with_llm.py`
  - `run_ontology_v2.py`

- `mock` 模式在 `project_sale_new` 上验证通过。

- 用户授权后，使用 DeepSeek 真实模型验证通过：
  - provider：`openai-compatible`。
  - model：`deepseek-v4-pro`。
  - base_url：`https://api.deepseek.com`。
  - 项目：`project_sale_new`。
  - 精炼候选：5。
  - 成功：5。
  - 失败：0。
  - LLM 置信度分布：`high=3`，`medium=2`。

- 抽查效果：
  - `本金/保证金` 被精炼为“场外衍生品名义本金与保证金”，并建议拆成“名义本金”和“保证金”。
  - `时间/生命周期` 被精炼为“合约生命周期关键日期”，并提示需要区分业务日期与系统 ETL 日期。
  - `交易对手/客户` 被精炼为“交易对手”，并提示需要确认“交易对手”和“客户”是否等价。
  - `合约/协议` 被精炼为“OTC 衍生品公司销售协议”，强证据集中在 `t98_otc_deri_comp_sale_info` 系列表。
  - `费率/费用/收益率` 被精炼为“场外衍生品合约费率/费用/收益率”，并建议按费率、费用、收益率或按期权/TRS 拆分。

## 2026-08-12 Ontology v2：表主题、字段组、血缘验证与跨表对齐

### 变更

- 新增 `ontology_v2_utils.py`
  - 提供图谱加载、文本清洗、HTML 标签清理、分词、同义词归一、概念关键词识别和 JSON/JSONL 写入工具。

- 新增 `build_table_profiles.py`
  - 基于 `Dataset`、`Column`、`PRODUCES`、`CONSUMES`、`READS`、`DATASET_DEPENDS_ON` 生成表主题画像。
  - 识别表角色：结果事实表、源同步表、主数据表、参数配置表、关系映射表、中间表、事件日志表等。
  - 输出 `table_profiles.jsonl` 和可选入图事实。

- 新增 `discover_field_groups.py`
  - 在单表内按字段注释、字段概念、表主题和字段血缘触点归纳 `SemanticFieldGroup`。
  - 支持合约/协议、交易对手/客户、人员/机构/归属、产品/标的、销售收入/创收、费率/费用/收益率、本金/保证金等字段组。
  - 对同一表内重复字段名做去重，降低由元数据列和推断列重复带来的噪声。

- 新增 `verify_concept_evidence.py`
  - 用字段级 `DERIVED_FROM` / `INFLUENCED_BY` 和表级上下游验证字段组证据。
  - 输出字段组证据等级：`strong`、`medium`、`weak`。
  - 生成字段组之间的 `DERIVED_FROM_GROUP` 候选关系。

- 新增 `align_concepts.py`
  - 将不同表中的字段组按概念、表主题、共享上下游和字段组派生关系对齐为 `ConceptCandidate`。
  - 输出关系类型：`derived_variant`、`same_source_variant`、`same_concept_candidate`、`weak_related`。
  - 对 `other_*` 弱语义组增加证据约束，避免噪声过度对齐。

- 新增 `run_ontology_v2.py`
  - 一键执行表主题识别、字段组归纳、血缘验证、跨表概念对齐四步。
  - 输出 `ontology_v2_manifest.json`。

- 更新文档：
  - `TECHNICAL_SOLUTION.md` 新增 `ontology_v2/` 路线说明。
  - `ONTOLOGY_DISCOVERY_PRACTICE.md` 增加表主题与字段组发现实践结果。

### 验证

- 编译通过：
  - `ontology_v2_utils.py`
  - `build_table_profiles.py`
  - `discover_field_groups.py`
  - `verify_concept_evidence.py`
  - `align_concepts.py`
  - `run_ontology_v2.py`

- 在 `project_sale_new` 上验证通过：
  - 表画像：370。
  - 字段组：675。
  - 强证据字段组：134。
  - 字段组派生关系：311。
  - 跨表概念候选：78。
  - 跨表候选关系：2929。

- 抽查结果：
  - 能发现“场外衍生品销售日报”“合约/协议”“交易对手/客户”“产品/标的”“销售收入/创收”“费率/费用/收益率”“本金/保证金”等候选业务对象。
  - 对没有指标节点的 `project_sale_new`，`ontology_v2` 比单字段口径族发现更能解释项目业务结构。

## 2026-08-05 Ontology/口径族候选发现第一版

### 变更

- 新增 `discover_concept_families.py`
  - 基于现有图谱事实发现指标和字段的口径族候选。
  - 优先读取 `strategy_llm_graph_nodes.jsonl` / `strategy_llm_graph_edges.jsonl`，没有 LLM 图事实时回退到 `strategy_graph_nodes.jsonl` / `strategy_graph_edges.jsonl`。
  - 支持 `--project-key` 覆盖输出项目标识，便于和多项目注册名对齐。
  - 基于名称、登记口径、代码口径、共同上游、共同下游、任务重叠和派生桥接计算候选分数。
  - 输出候选关系类型：`same_definition_candidate`、`same_concept_candidate`、`same_source_variant`、`derived_variant`、`conflict_candidate`、`weak_related`。
  - 默认不直接纳入正式知识，候选均标记为 `knowledge_admission=needs_review`。

- 新增 ontology 输出目录约定：
  - `ontology_summary.json`
  - `concept_candidates.json`
  - `metric_families.json`
  - `column_families.json`
  - `relationship_candidates.json`
  - `review_queue.json`
  - `ontology_graph_nodes.jsonl`
  - `ontology_graph_edges.jsonl`

- 更新 `TECHNICAL_SOLUTION.md`
  - 在总体架构中增加 Ontology 候选层。
  - 新增“Ontology 与口径族候选发现”章节，说明输入证据、发现逻辑、输出产物和入图原则。

### 实践

- `digital_operations`
  - 指标画像：319。
  - 指标候选关系：79。
  - 指标口径族候选：38。
  - 字段画像：39820。
  - 字段候选关系：6000。
  - 字段口径族候选：774。

- `t0`
  - 指标画像：207。
  - 指标候选关系：23。
  - 指标口径族候选：10。
  - 字段画像：97470。
  - 字段候选关系：8000。
  - 字段口径族候选：1952。

- `sale`
  - 指标画像：290。
  - 指标候选关系：27。
  - 指标口径族候选：13。
  - 字段画像：62410。
  - 字段候选关系：8000。
  - 字段口径族候选：1186。

### 验证

- 编译通过：`discover_concept_families.py`。
- 三个项目均成功生成 `ontology/` 产物。
- 抽查 top 指标口径族候选，已能发现如“场内交易金额”“风险测评办理次数”“T0 净佣金创收”“KPI 考核收入”“净资产变体”等可复核关系。

## 2026-07-30 局部知识图谱展示工具

### 变更

- `query_api.py`
  - 为展示页文件模式访问接口增加最小 CORS/OPTIONS 支持。
  - 解决 `file://.../kg_graph_showcase.html` 调用 `http://127.0.0.1:8790/api/...` 时浏览器报 `Load failed` 的问题。

- `reports/kg_graph_showcase.html`
  - 文件模式下默认接口地址为 `http://127.0.0.1:8790`。
  - 查询失败时展示更明确的接口连接诊断信息。
  - 项目列表在 `/api/projects` 不可用时使用内置项目兜底，避免项目下拉为空。
  - 新增局部图放大、缩小、适配、滚轮缩放和拖动画布能力，便于汇报时查看大子图细节。

- `query_layer/service.py`
  - 新增 `get_graph_neighborhood` 查询原语。
  - 支持从任务、表、字段、指标等任意实体出发，按 `direction`、`max_hops`、`relation_profile` 展开局部图。
  - 返回 `data.visual_graph.nodes` 和 `data.visual_graph.edges`，面向页面、机器人和大模型上下文压缩复用。
  - 内置关系范围：`schedule`、`dataset_lineage`、`column_lineage`、`code`、`metric`、`lineage`、`all_safe`。
  - 对边类型做白名单校验，避免 `OWNS`、`BELONGS_TO_LAYER` 等展示干扰关系误入默认局部图。

- `query_api.py`
  - 新增 `POST /api/graph/neighborhood`。
  - 新增展示页入口：`GET /showcase` 或 `GET /kg-showcase`。

- `reports/kg_graph_showcase.html`
  - 新增轻量汇报展示页。
  - 自动读取 `/api/projects` 展示已导入项目概览。
  - 支持选择项目、输入锚点、配置方向/深度/关系范围，并绘制局部知识图谱。

- `test_query_layer.py`
  - 新增局部图查询单测。
  - 覆盖可视化边方向、白名单拦截等核心行为。

### 验证

- 查询层单测通过：`10 tests OK`。
- 编译通过：
  - `query_layer/service.py`
  - `query_api.py`
- 真实 Neo4j 只读冒烟通过：
  - 已识别项目：`digital_operations`、`iresearch`、`project_customer_report`、`project_sale_new`、`project_stastic_month`、`sale`、`t0`、`trial_project`。
  - `iresearch` 精确字段 `upper_grp_name` 可展开局部图，返回 `visual_graph`。

## 2026-07-29 iresearch 项目图谱化与大图影响分析修复

### 变更

- `query_layer/service.py`
  - 修复 `analyze_impact` 在大图上的路径展开问题。
  - 原实现部分查询写死 `*1..50` 再用 `WHERE length(path) <= $hops` 或 `size(rels) <= $hops` 过滤，大图上会造成不必要的宽展开。
  - 当前改为直接按请求的 `max_hops` 拼接路径上限，例如 `max_hops=3` 时只查询 `*1..3`。

- `test_query_layer.py`
  - 新增 `test_impact_groups_respects_requested_hops`，防止影响分析分组查询再次退回宽展开。

- `iresearch` 项目图谱
  - 使用最新列血缘增强重新解析并构图。
  - 暂不执行任务/表级 LLM 摘要。
  - 已导入 Neo4j，项目 ID：`iresearch`。

### 验证

- `iresearch` 图谱导入 Neo4j 后数量一致：
  - 节点：`87884`
  - 边：`361657`
- 查询层验证通过：
  - `analyze_impact` 可返回 `impact_groups` 和 `impact_explanations`。
  - `resolve_entity` ambiguous 场景可返回消歧上下文。
  - 知识质量属性已入库。
- 编译通过：`query_layer/service.py`。
- 查询层单测通过：`8 tests OK`。

## 2026-07-28 查询层、知识质量和摘要增强

### 变更

- `query_layer/service.py`
  - `analyze_impact` 增加 `impact_groups` 和 `impact_explanations`。
  - 字段影响按直接值来源、过滤、关联、分组、HAVING、QUALIFY、排序等原因分组。
  - `resolve_entity` 增加候选实体消歧上下文。
  - ambiguous 返回中增加 `clarification.needed_context` 和建议追问问题。
  - 新增 `get_recent_changes` 原语，读取项目 `incremental/changes/*.json` 运行态变化事件。
  - `get_task_context` 和 `get_dataset_context` 支持读取本地任务/表摘要产物。

- `query_api.py`
  - 新增 `/api/changes/recent`。

- `build_graph_facts.py`
  - 节点和边增加知识质量属性：
    - `quality_score`
    - `quality_tier`
    - `knowledge_admission`

- `generate_asset_summaries.py`
  - 新增任务级和表级摘要生成脚本。
  - 支持 `mock` 和 `openai-compatible` provider。
  - 输出 `llm/task_summaries.jsonl` 和 `llm/dataset_summaries.jsonl`。

- 文档
  - 更新 `TECHNICAL_SOLUTION.md`、`QUERY_LAYER_DESIGN.md`、`QUERY_API_USAGE.md`。
  - 同步当前 HTTP API、13 个业务查询原语、影响分析分组、实体消歧上下文、增量变化查询和任务/表摘要能力。

### 验证

- 编译通过：
  - `query_layer/service.py`
  - `query_api.py`
  - `build_graph_facts.py`
  - `generate_asset_summaries.py`
  - `run_project_pipeline.py`
- 查询协议 JSON 校验通过：`query_layer/query_schemas.json`。
- 查询层单测通过：`7 tests OK`。
- 流水线 dry-run 通过，确认 `--build-asset-summaries` 会按预期挂载 `generate_asset_summaries` 步骤。

## 2026-07-24 字段间接影响血缘

### 变更

- `extract_column_lineage.py`
  - 新增 `column_influence` 事实抽取。
  - 在 SQL 上下文中识别影响结果但不直接作为字段值来源的字段：
    - `WHERE`
    - `JOIN ON`
    - `JOIN USING`
    - `GROUP BY`
    - `HAVING`
    - `QUALIFY`
    - `ORDER BY`
  - 输出 `strategy_column_influence.json`。
  - 在汇总中增加 `column_influence_count`、`influence_type_distribution` 和 `influence_source_resolution_distribution`。

- `build_graph_facts.py`
  - 新增 `Column -[:INFLUENCED_BY]-> Column` 边。
  - 保持 `DERIVED_FROM` 只表达直接字段来源，`INFLUENCED_BY` 表达过滤、关联、分组等间接影响。

- `query_layer/service.py`
  - 字段上下游追踪和 `analyze_impact` 纳入 `INFLUENCED_BY`。
  - 路径和证据返回中补充 `influence_type`。

- `report_project.py`、`audit_graph_facts.py`
  - 增加间接影响字段事实统计和审计计数。

### 验证

- 编译通过：
  - `extract_column_lineage.py`
  - `build_graph_facts.py`
  - `report_project.py`
  - `audit_graph_facts.py`
  - `query_layer/service.py`
- 查询层单测通过：`6 tests OK`。
- 内存 SQL 样例验证可识别：
  - `filter`
  - `group_by`
  - `having`
  - `join_condition`

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
