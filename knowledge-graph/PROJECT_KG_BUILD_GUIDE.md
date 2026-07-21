# 从结果任务 ID 构建项目知识图谱操作指引

本文档面向拿到 `kg_probe_gf` 代码包的项目同事。假设同事已经有自己项目的结果表调度 ID，希望在内网环境中采集项目代码和元数据，并生成可查询的项目知识图谱。

## 1. 总体流程

一个项目从结果任务 ID 到可导入图谱，分为 5 步：

1. 准备运行环境。
2. 准备项目任务 ID。
3. 配置并验证 Horae、SzConnector。
4. 执行项目流水线，生成图谱文件。
5. 导入 Neo4j。

核心命令入口是：

```bash
python3 kg_probe/run_project_pipeline.py
```

Neo4j 导入入口是：

```bash
python3 kg_probe/import_and_validate_neo4j.py
```

## 2. 准备运行环境

### 2.1 解压代码包

假设代码包放在：

```text
/path/to/kg_probe_gf
```

进入代码包目录：

```bash
cd /path/to/kg_probe_gf
```

目录中应至少包含：

```text
kg_probe/
requirements.txt
README.md
PROJECT_PIPELINE.md
QUERY_API_USAGE.md
```

### 2.2 安装 Python 依赖

如果环境可以联网：

```bash
python3 -m pip install -r requirements.txt
```

如果是公司内网离线环境，需要提前准备离线 wheel 包，再安装。

至少需要：

```text
sqlglot
neo4j
```

### 2.3 Windows Git Bash 注意事项

如果在 Windows Git Bash 中运行，建议命令使用：

```bash
python -X utf8 ...
```

这样可以避免 Horae 或 SzConnector 输出中文、emoji 时触发编码错误。

例如：

```bash
python -X utf8 kg_probe/run_project_pipeline.py --help
```

## 3. 准备项目任务 ID

### 3.1 选择结果任务 ID

项目同事需要准备“结果表调度 ID”，也就是这个项目最终产出表或核心结果表对应的调度任务 ID。

如果一个项目有多个结果表，就准备多个任务 ID。

示例：

```text
241721
172840
```

### 3.2 建议使用任务文件

新建一个任务文件，例如：

```text
my_project_task_ids.txt
```

内容可以一行一个 ID：

```text
241721
172840
```

也可以逗号分隔：

```text
241721,172840
```

建议一行一个 ID，方便维护。

## 4. 验证 Horae 和 SzConnector

项目流水线依赖两个内部工具：

- `horae`：采集调度任务详情、上下游依赖、任务页面 SQL/配置、运行日志。
- `szconnector`：采集表元数据、字段元数据、指标登记口径。

先确认命令存在：

```bash
which horae
which szconnector
```

如果命令不可用，需要先安装或联系工具维护方。

### 4.1 认证信息

Horae 和 SzConnector 一般依赖内网登录态、cookie 或 token。具体配置方式取决于公司内部工具版本。

运行前至少要确认：

- `horae` 可以查询一个已知任务。
- `szconnector` 可以查询一张已知表或一个已知指标。

如果工具提示未登录、token 过期、cookie 失效，需要先重新登录或更新对应配置。

## 5. 执行冒烟采集

第一次不要直接跑很深，建议先用一个任务 ID 做小深度冒烟。

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id smoke_my_project \
  --tasks 241721 \
  --max-depth 2 \
  --max-nodes 100 \
  --skip-sz-metadata \
  --column-lineage-passes 0
```

Windows Git Bash 建议：

```bash
python -X utf8 kg_probe/run_project_pipeline.py \
  --project-id smoke_my_project \
  --tasks 241721 \
  --max-depth 2 \
  --max-nodes 100 \
  --skip-sz-metadata \
  --column-lineage-passes 0
```

冒烟成功后，输出目录通常是：

```text
artifacts/projects/smoke_my_project
```

如果没有指定 `--output-root`，以当前运行目录下的 `artifacts/projects/<project_id>` 为准。

需要重点检查：

```text
lineage.json
task_details.json
code_artifacts_page.json
log_artifacts_full.json
strategy_sql_statements.json
strategy_graph_nodes.jsonl
strategy_graph_edges.jsonl
strategy_fact_audit.json
strategy_quality_report.json
```

如果这些文件都存在，且 `strategy_quality_report.json` 中没有严重采集失败，再跑完整项目。

## 6. 执行完整项目构建

### 6.1 不带 LLM 的基础图谱

适合先验证采集、SQL 解析、字段血缘和图谱构建。

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file my_project_task_ids.txt \
  --max-depth 8000 \
  --max-nodes 8000 \
  --column-lineage-passes 2
```

参数说明：

- `--project-id`：项目唯一标识，后续查询接口也用这个值。
- `--task-file`：项目结果任务 ID 文件。
- `--max-depth`：向上穿透最大深度。
- `--max-nodes`：最多采集任务节点数。
- `--column-lineage-passes`：字段血缘增强轮数，建议先用 `2`。

### 6.2 构建 LLM 指标口径层

LLM 指标口径层是可选增强层，不影响基础调度血缘、表血缘和字段血缘构建。

它的作用是：

- 基于 SQL、表、字段、任务和登记口径证据，生成“代码优先”的指标口径说明。
- 将代码口径与 SzConnector 中的登记口径进行比对，判断是否一致、部分一致、冲突、登记缺失或代码证据不足。
- 将比对结果入图，供后续回答“这个指标到底怎么算”“登记口径和代码是否一致”“哪些指标需要人工确认”等问题。
- 为每个指标保留证据包、模型名称、Prompt 模板版本和结构化输出，便于追溯。

适合启用 LLM 层的情况：

- 项目中包含较多指标表或指标登记信息。
- 希望支持业务口径答疑。
- 希望识别登记口径和代码实现不一致的指标。
- 后续要让业务老师或项目成员做人工口径确认。

可以暂不启用 LLM 层的情况：

- 当前只关心调度血缘、表血缘、字段血缘或下游影响分析。
- 项目没有指标登记信息。
- 当前运行环境不能访问大模型服务。

如果项目包含指标，并且希望生成“代码口径”和“登记口径比对”，增加：

```bash
--build-llm
```

默认是 `mock`，不会真实调用模型，只验证流程：

```bash
python3 kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file my_project_task_ids.txt \
  --max-depth 8000 \
  --max-nodes 8000 \
  --column-lineage-passes 2 \
  --build-llm
```

如果要真实调用 DeepSeek 或其他 OpenAI 兼容模型：

```bash
export OPENAI_API_KEY="你的API_KEY"
export LLM_BASE_URL="https://api.deepseek.com"
export LLM_MODEL="deepseek-v4-pro"

python3 kg_probe/run_project_pipeline.py \
  --project-id my_project \
  --task-file my_project_task_ids.txt \
  --max-depth 8000 \
  --max-nodes 8000 \
  --column-lineage-passes 2 \
  --build-llm \
  --llm-provider openai-compatible
```

如果公司环境不能访问外部模型，可以先不做 LLM 层，后续把采集产物带回可调用模型的环境再补跑。

## 7. 检查构建结果

完整构建后，项目目录一般是：

```text
artifacts/projects/my_project
```

基础图必须有：

```text
strategy_graph_nodes.jsonl
strategy_graph_edges.jsonl
strategy_fact_audit.json
strategy_quality_report.json
strategy_graph_query_validation.json
```

如果启用了 LLM，额外应有：

```text
llm/evidence_bundles.jsonl
llm/code_definition_requests.jsonl
llm/code_definitions.jsonl
llm/definition_comparisons.jsonl
strategy_llm_graph_nodes.jsonl
strategy_llm_graph_edges.jsonl
```

建议检查质量报告：

```bash
python3 -m json.tool artifacts/projects/my_project/strategy_quality_report.json | head -100
```

重点看：

- 是否有采集失败。
- SQL 解析数量是否合理。
- 图节点和边是否非空。
- 字段血缘错误是否在可接受范围内。
- `strategy_fact_audit.json` 是否存在缺失端点或必填属性缺失。

## 8. 导入 Neo4j

### 8.1 选择导入前缀

如果没有 LLM 层，导入：

```text
strategy
```

如果有 LLM 层，优先导入：

```text
strategy_llm
```

### 8.2 多项目导入

推荐使用 `--project-id` 和 `--replace-project`，这样只替换当前项目，不影响同一个 Neo4j 中的其他项目。

基础图导入：

```bash
python3 kg_probe/import_and_validate_neo4j.py \
  artifacts/projects/my_project \
  --prefix strategy \
  --project-id my_project \
  --replace-project
```

LLM 增强图导入：

```bash
python3 kg_probe/import_and_validate_neo4j.py \
  artifacts/projects/my_project \
  --prefix strategy_llm \
  --project-id my_project \
  --replace-project
```

如果 Neo4j 地址、账号或密码文件不同：

```bash
python3 kg_probe/import_and_validate_neo4j.py \
  artifacts/projects/my_project \
  --prefix strategy_llm \
  --project-id my_project \
  --replace-project \
  --uri bolt://localhost:7687 \
  --user neo4j \
  --password-file /path/to/neo4j_password.txt
```

导入完成后会生成：

```text
strategy_neo4j_validation.json
```

或：

```text
strategy_llm_neo4j_validation.json
```

## 9. 常见问题

### 9.1 UnicodeEncodeError 或 UnicodeDecodeError

Windows 环境优先使用：

```bash
python -X utf8 ...
```

### 9.2 Horae 或 SzConnector 认证失败

先单独验证内部工具是否可用。通常需要重新登录、更新 cookie 或 token。

### 9.3 `max_nodes_reached`

说明上游穿透达到了 `--max-nodes` 限制，不等于某个任务没有上游。

处理方式：

- 如果该分支对项目不重要，可以记录并接受。
- 如果重要，提高 `--max-nodes` 后重跑。

### 9.4 字段血缘不是 100%

字段血缘是保守解析，不会为了追求覆盖率而生成不可靠边。

常见未解析原因：

- 动态 SQL。
- 多层临时表链缺少可识别 schema。
- 字段未限定且多表存在同名字段。
- DMS 缺少精确字段元数据。

### 9.5 某些项目没有指标

如果项目没有 `Metric` 或 `MetricDefinition`，通常说明结果表没有匹配到指标登记信息。这不影响调度、SQL、表和字段血缘图谱使用。

### 9.6 LLM 口径显示代码证据不足

表示当前图谱证据不足以让模型给出确定口径。可以后续通过人工口径修正文件补充。

### 9.7 同一个 Neo4j 导入多个项目会不会冲突

不会。使用 `--project-id` 和 `--replace-project` 时，节点 ID 会加项目名前缀，并按 `project_key` 隔离查询。

## 10. 建议交付物

如果项目同事要把结果交给别人查询，建议至少交付：

- `kg_probe_gf` 代码包。
- 项目图谱目录，例如 `artifacts/projects/my_project`。
- Neo4j 中已导入的项目 `project_id`。
- `QUERY_API_USAGE.md`。
- 如果没有共享 Neo4j，则还需要提供 `strategy_graph_nodes.jsonl`、`strategy_graph_edges.jsonl` 或 `strategy_llm_graph_nodes.jsonl`、`strategy_llm_graph_edges.jsonl` 供重新导入。
