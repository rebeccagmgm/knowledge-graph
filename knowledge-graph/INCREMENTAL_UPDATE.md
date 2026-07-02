# 增量更新

当前版本采用“轻量检测、变化后整项目重建”的策略。它不直接局部修改Neo4j，避免扫描或解析失败导致正式图处于半更新状态。

## 登记项目

复制`project_registry.example.json`为运行环境中的登记文件。每个项目配置：

- `project_id`：项目唯一标识。
- `result_task_ids`：用户登记的结果任务ID。
- `supplemental_task_ids`：调度依赖无法自动发现的补充任务。
- `scan_interval_hours`：扫描间隔，默认48小时。
- `build_llm`：变化后是否重新运行LLM口径层，默认关闭。
- `import_neo4j`：重建成功后是否导入Neo4j，默认关闭。

实际登记文件默认位置：

```text
project_registry.json
```

## 初始化基线

```bash
python3 kg_probe/incremental_update.py \
  --project-id trial_project \
  --initialize
```

## 扫描

扫描单个项目：

```bash
python3 kg_probe/incremental_update.py \
  --project-id trial_project
```

扫描登记表中的全部启用项目：

```bash
python3 kg_probe/incremental_update.py --all
```

建议定时器每天调用一次`--all`。扫描器会根据各项目的`scan_interval_hours`判断是否到期，因此默认每48小时真正访问一次Horae。

只检测、不触发重建：

```bash
python3 incremental_update.py --project-id trial_project --no-rebuild
```

离线验证当前快照：

```bash
python3 incremental_update.py --project-id trial_project --offline --force-scan
```

## 检测范围

- 上游任务新增或删除。
- 调度依赖边变化。
- 任务类型、负责人等元数据变化。
- 任务代码原始文本变化。
- SQL规范化后的语义变化。
- 已采集表Schema变化。
- 已采集指标登记变化。

仅空格、换行和大小写变化会记录为`text_only_changed_task_ids`，不会触发重建。

## 输出

每个项目会生成：

```text
incremental/current_snapshot.json
incremental/state.json
incremental/changes/<时间>.json
incremental/scan.lock
```

`ChangeSet`包含变化任务、依赖边、受影响下游任务和受影响指标。

正式比较前会执行采集质量门禁。入口缺失、血缘错误、任务详情失败、页面代码请求失败、Hive日志缺失或日志请求失败时，本轮写入`incremental/failures/`并停止，不更新基线、不重建图谱。

如果项目目录中存在`manual_metric_overrides.json`，受影响指标的人工补充会被标记为：

```json
{
  "needs_review": true,
  "review_reason": "related_task_or_code_changed"
}
```

## 当前边界

- 第一版检测到语义变化后调用完整项目流水线，不做任务级Neo4j局部更新。
- 表Schema和指标登记Hash会被比较，但只有元数据被重新采集后才能发现变化。
- 默认不重新调用LLM，也不导入Neo4j；需要在项目登记中显式开启。
- Hive任务以最新成功实例日志为运行态代码来源。
