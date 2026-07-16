# 变更日志

## 2026-07-14 字段血缘生成表达式建模

### 变更内容

- `extract_column_lineage.py`
  - 将字面量和生成表达式类 projection 从错误中重新归类为可解释的字段事实。
  - 为生成字段新增 `generation_type`：
    - `literal`：固定值，例如 `'RCC' AS data_src_cd` 或 `'' AS remark`。
    - `generated_expression`：不依赖来源字段的表达式，例如 `from_unixtime(unix_timestamp()) AS data_time`。
  - 将无法解析来源字段的失败类型重命名为 `projection_without_resolved_source_column`。
  - 增加本地 SQL 路径回退能力，使 Windows 内网采集产物拷回 macOS 后仍可重新处理。
  - 扩展 Hive 日志噪声过滤，覆盖常见执行输出片段。

- `build_graph_facts.py`
  - 新增 `GeneratedExpression` 节点。
  - 新增 `Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression` 边。
  - 保持真实来源字段血缘仍使用 `Column -[:DERIVED_FROM]-> Column`。

- `report_project.py`
  - 新增生成字段数量和生成类型分布统计。
  - 新增 `explainable_column_fact_count` 和 `explainable_column_fact_pct`，同时覆盖已解析来源字段和生成字段。

- `audit_graph_facts.py`
  - 新增 `generated_by_expression_count`。

### `new_project` 验证结果

- 优化前：
  - 字段血缘事实：`54035`
  - 字段血缘错误：`19993`
  - `projection_without_source_column`：`16225`
  - 图节点：`90191`
  - 图边：`151233`

- 优化后：
  - 字段血缘事实：`70260`
  - 明确来源字段事实：`23510`
  - 生成字段事实：`16225`
    - `literal`：`15297`
    - `generated_expression`：`928`
  - 字段血缘错误：`3768`
  - `projection_without_source_column`：已从错误分布中移除。
  - 图节点：`117802`
  - 图边：`178844`
  - `GeneratedExpression` 节点：`16225`
  - `GENERATED_BY_EXPRESSION` 边：`16225`
  - 图谱审计：无缺失端点，无必填属性缺失。

## 2026-07-14 Hive 到外部库表同步血缘

### 变更内容

- `build_graph_facts.py`
  - 对 `hive2*` 任务，不再将任务名或描述当作产出表。
  - 使用 `sync_info["Hive源库"] + sync_info["Hive源表"]` 作为被消费的 Hive 数据集。
  - 使用 `sync_info["目标库表"]` 作为产出的外部数据集。

- `extract_column_lineage.py`
  - 对 `hive2*` 任务，将 Hive 源字段映射到外部目标表字段。
  - 为该映射方式新增 `target_resolution = "task_sync_target"`。
  - 保持来源数据集为 Hive 表，目标数据集为非 Hive 表，例如 PostgreSQL 或 Oracle 目标表。

### `new_project` 验证结果

- `ambiguous_task_outputs`：从 `9` 降为 `0`。
- `task_sync_target` 字段事实：`412`。
- 示例：
  - `dm_om_n.wt_cust_emp_dev_rela_info.cust_id`
  - `-> aumcrmii.mv_khxx_khgx.cust_id`
  - 通过任务 `207818` 建立。
- 已移除由任务名造成的错误产出表：
  - `hive2pg.wt_cust_emp_dev_rela_info`
  - `crmii.erp_a_gf_emp_info_v_new`
  - `src_gfjgj.erp_a_gf_*_kxc`

## 2026-07-14 CTAS/UNION 与星号展开增强

### 变更内容

- `extract_column_lineage.py`
  - 为 `CREATE TABLE ... AS SELECT ...` 增加回退解析能力，处理 sqlglot 将其归类为 `Command` 的场景。
  - 为 `UNION` / `UNION ALL` 增加按分支感知的 projection 抽取。
  - 增加 CTAS 目标表识别。CTAS 字段血缘现在指向 SQL 创建的表，而不是退回任务级产出表。
  - 增加 CTAS schema 推断，使后续语句可展开 `TEMP.xxx B` 中的 `B.*`。
  - 增加简单子查询别名映射，支持 `A.*` 等场景。
  - 为未带库名的表引用增加唯一表名后缀匹配。
  - 标准化字段名前清理 DMS 字段名中的 HTML 标签。

- `build_graph_facts.py`
  - 在生成的边 ID 和边属性中加入 `branch_ordinal`，避免 `UNION` 不同分支互相覆盖。

### `new_project` 验证结果

- 本次变更前，即已完成生成表达式和 `hive2*` 同步修复后：
  - 字段血缘事实：`70392`
  - 明确来源字段事实：`23619`
  - 生成字段事实：`16225`
  - 错误数：`3763`
  - `schema_star_expand`：`1043`

- 本次变更后：
  - 字段血缘事实：`84175`
  - 明确来源字段事实：`51193`
  - 生成字段事实：`17566`
  - 错误数：`3242`
  - `schema_star_expand`：`8239`
  - `ctas_target` 事实：`28279`
  - 推断出的 CTAS schema：`645`
  - 图节点：`137369`
  - 图边：`226877`
  - `DERIVED_FROM` 边：`51193`
  - `GENERATED_BY_EXPRESSION` 边：`17566`
  - 图谱审计：无缺失端点，无必填属性缺失。

### 已解决样例

- `100514_b24c0c48a5c87d92`
  - 原先：`no_select_projection`
  - 现在：跨 CTAS/UNION 分支生成 `54` 条事实，`0` 个错误。

- `100514_a045e42c69661101`
  - 原先：`A.*` 和 `B.*` 触发 `projection_without_output_name`
  - 现在：生成 `31` 条事实，`0` 个错误。
  - `A.*` 从 `pdata_n.t01_pty_stati_info_h` 展开。
  - `B.*` 从推断出的 `temp.t01_pty_stati_info_h_temp_rcc003` schema 展开。
