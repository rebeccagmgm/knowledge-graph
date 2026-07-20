# Change Log

## 2026-07-14 Column Lineage Generated Expressions

### Changed

- `extract_column_lineage.py`
  - Reclassified literal and generated projections from errors into explainable column facts.
  - Added `generation_type` for generated fields:
    - `literal`: fixed values such as `'RCC' AS data_src_cd` or `'' AS remark`.
    - `generated_expression`: expressions without source columns, such as `from_unixtime(unix_timestamp()) AS data_time`.
  - Renamed unresolved source-column failures to `projection_without_resolved_source_column`.
  - Added local SQL path fallback so artifacts collected on Windows can be reprocessed after being copied back to macOS.
  - Expanded Hive log noise filtering for common execution-output fragments.

- `build_graph_facts.py`
  - Added `GeneratedExpression` nodes.
  - Added `Column -[:GENERATED_BY_EXPRESSION]-> GeneratedExpression` edges.
  - Kept true source-field lineage as `Column -[:DERIVED_FROM]-> Column`.

- `report_project.py`
  - Added generated-column counts and generation-type distribution.
  - Added `explainable_column_fact_count` and `explainable_column_fact_pct`, covering both resolved source fields and generated fields.

- `audit_graph_facts.py`
  - Added `generated_by_expression_count`.

### Validation On `new_project`

- Before:
  - Column lineage facts: `54035`
  - Column lineage errors: `19993`
  - `projection_without_source_column`: `16225`
  - Graph nodes: `90191`
  - Graph edges: `151233`

- After:
  - Column lineage facts: `70260`
  - Derived source-column facts: `23510`
  - Generated column facts: `16225`
    - `literal`: `15297`
    - `generated_expression`: `928`
  - Column lineage errors: `3768`
  - `projection_without_source_column`: removed from error distribution.
  - Graph nodes: `117802`
  - Graph edges: `178844`
  - `GeneratedExpression` nodes: `16225`
  - `GENERATED_BY_EXPRESSION` edges: `16225`
  - Graph audit: no missing endpoints, no missing required properties.

## 2026-07-14 Hive-To-External Sync Lineage

### Changed

- `build_graph_facts.py`
  - For `hive2*` tasks, no longer treats task name/description as an output dataset.
  - Uses `sync_info["Hive源库"] + sync_info["Hive源表"]` as the consumed Hive dataset.
  - Uses `sync_info["目标库表"]` as the produced external dataset.

- `extract_column_lineage.py`
  - For `hive2*` tasks, maps selected Hive fields to external target-table fields.
  - Adds `target_resolution = "task_sync_target"` for this mapping mode.
  - Keeps the source dataset as the Hive table and the target dataset as the non-Hive table, such as PostgreSQL or Oracle target tables.

### Validation On `new_project`

- `ambiguous_task_outputs`: reduced from `9` to `0`.
- `task_sync_target` column facts: `412`.
- Example:
  - `dm_om_n.wt_cust_emp_dev_rela_info.cust_id`
  - `-> aumcrmii.mv_khxx_khgx.cust_id`
  - via task `207818`.
- False output datasets caused by task names were removed for the checked sync tasks:
  - `hive2pg.wt_cust_emp_dev_rela_info`
  - `crmii.erp_a_gf_emp_info_v_new`
  - `src_gfjgj.erp_a_gf_*_kxc`

## 2026-07-14 CTAS/UNION And Star Expansion

### Changed

- `extract_column_lineage.py`
  - Added fallback parsing for `CREATE TABLE ... AS SELECT ...` statements that sqlglot classifies as `Command`.
  - Added branch-aware projection extraction for `UNION` / `UNION ALL`.
  - Added CTAS target detection. Column lineage for CTAS now targets the created table instead of falling back to task-level output.
  - Added inferred CTAS schema extraction so later statements can expand temporary tables such as `TEMP.xxx B` in `B.*`.
  - Added subquery-alias mapping for simple subqueries, enabling cases such as `A.*`.
  - Added unique table-suffix schema matching for unqualified table references.
  - Cleaned HTML tags from DMS column names before normalizing columns.

- `build_graph_facts.py`
  - Included `branch_ordinal` in generated edge IDs and edge properties so `UNION` branches do not overwrite each other.

### Validation On `new_project`

- Before this change, after generated-expression and hive2 sync fixes:
  - Column lineage facts: `70392`
  - Derived source-column facts: `23619`
  - Generated column facts: `16225`
  - Errors: `3763`
  - `schema_star_expand`: `1043`

- After:
  - Column lineage facts: `84175`
  - Derived source-column facts: `51193`
  - Generated column facts: `17566`
  - Errors: `3242`
  - `schema_star_expand`: `8239`
  - `ctas_target` facts: `28279`
  - Inferred CTAS schemas: `645`
  - Graph nodes: `137369`
  - Graph edges: `226877`
  - `DERIVED_FROM` edges: `51193`
  - `GENERATED_BY_EXPRESSION` edges: `17566`
  - Graph audit: no missing endpoints, no missing required properties.

### Resolved Examples

- `100514_b24c0c48a5c87d92`
  - Previously: `no_select_projection`
  - Now: `54` facts, `0` errors across CTAS/UNION branches.

- `100514_a045e42c69661101`
  - Previously: `projection_without_output_name` for `A.*` and `B.*`
  - Now: `31` facts, `0` errors.
  - `A.*` expands from `pdata_n.t01_pty_stati_info_h`.
  - `B.*` expands from inferred schema of `temp.t01_pty_stati_info_h_temp_rcc003`.
