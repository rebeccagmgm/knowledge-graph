# Ontology v2 发现实践摘要

## 目标

当前 ontology 发现只保留第二版路线：

```text
表主题识别 -> 字段组语义归纳 -> 血缘证据验证 -> 跨表概念对齐 -> 可选 LLM 精炼
```

它的目标不是直接生成正式 ontology，而是从项目图谱中反向发现候选业务对象、业务属性、度量概念、参考数据和跨表概念关系，为业务专家复核和后续语义层建设提供证据。

## 执行方式

基础运行：

```bash
python3 kg_probe/run_ontology_v2.py <project_dir> \
  --prefix strategy \
  --project-key <project_id>
```

启用 DeepSeek 精炼：

```bash
python3 kg_probe/run_ontology_v2.py <project_dir> \
  --prefix strategy \
  --project-key <project_id> \
  --refine-ontology-llm \
  --llm-provider openai-compatible \
  --llm-model deepseek-v4-pro \
  --llm-base-url https://api.deepseek.com
```

内部步骤：

| 步骤 | 脚本 | 作用 |
| --- | --- | --- |
| 表主题识别 | `build_table_profiles.py` | 判断表角色、业务主题、上下游和字段概念分布 |
| 字段组语义归纳 | `discover_field_groups.py` | 在单表内归纳合约/协议、交易对手、产品、收入、费率、本金等字段组 |
| 血缘路径验证 | `verify_concept_evidence.py` | 用字段级和表级血缘给字段组打证据等级 |
| 跨表概念对齐 | `align_concepts.py` | 将不同表中的字段组对齐成候选业务概念 |
| LLM 精炼 | `refine_ontology_concepts_with_llm.py` | 对候选概念做命名、类型、边界、拆并建议和复核问题整理 |

## 输入证据

优先读取：

```text
strategy_llm_graph_nodes.jsonl
strategy_llm_graph_edges.jsonl
```

没有 LLM 图谱产物时回退读取：

```text
strategy_graph_nodes.jsonl
strategy_graph_edges.jsonl
```

主要使用：

- 表、字段、字段注释和表字段元数据。
- 表级读写、产出、依赖关系。
- 字段级直接血缘和间接影响血缘。
- 指标登记口径、代码口径和口径比对事实。
- 节点和边上的质量分、置信度和入图状态。

## 输出产物

默认输出目录：

```text
ontology_v2/
```

核心产物：

| 文件 | 用途 |
| --- | --- |
| `table_profiles.jsonl` | 表主题画像 |
| `field_groups.jsonl` | 表内字段组 |
| `verified_field_groups.jsonl` | 带血缘证据等级的字段组 |
| `field_group_relations.jsonl` | 字段组之间的派生关系 |
| `concept_candidates.jsonl` | 跨表候选概念 |
| `concept_relations.jsonl` | 跨表字段组候选关系 |
| `llm_refined_concepts.jsonl` | LLM 精炼后的候选概念解释 |
| `ontology_llm_refinement_summary.json` | LLM 精炼统计 |
| `*_graph_nodes.jsonl` / `*_graph_edges.jsonl` | 可选入图事实 |
| `ontology_v2_manifest.json` | 运行清单 |

## 实践结果

### `project_sale_new`

| 项 | 数量 |
| --- | ---: |
| 表画像 | 370 |
| 字段组 | 675 |
| 强证据字段组 | 134 |
| 字段组派生关系 | 311 |
| 跨表概念候选 | 78 |
| 跨表候选关系 | 2,929 |
| DeepSeek 精炼候选 | 5 |
| LLM 成功 | 5 |

主要发现：

- 项目围绕场外衍生品销售日报展开。
- 发现了合约/协议、交易对手/客户、产品/标的、销售收入/创收、费率/费用/收益率、本金/保证金等候选概念。
- LLM 精炼能进一步指出“名义本金”和“保证金”应拆分，“费率、费用、收益率”不宜混成一个正式概念。

详细报告：

```text
PROJECT_SALE_NEW_ONTOLOGY_V2_LLM_REPORT.md
```

### `t0`

| 项 | 数量 |
| --- | ---: |
| 表画像 | 5,335 |
| 字段组 | 8,590 |
| 强证据字段组 | 2,601 |
| 字段组派生关系 | 2,877 |
| 跨表概念候选 | 60 |
| 跨表候选关系 | 12,000 |
| DeepSeek 精炼候选 | 15 |
| LLM 成功 | 15 |

主要发现：

- `t0` 不是狭义的 T0 交易项目，而是覆盖客户、产品、员工机构、销售关系、创收收入、资产净值、两融保证金和 T0 交易表现的综合经营指标资产集合。
- LLM 精炼把部分规则粗类做了纠偏，例如把 `合约/协议` 下的候选识别为“经纪客户”“限售股卖出控制信息”“资产账户代理协议”等更准确的业务概念。
- 当前最值得继续细化的是 T0 交易表现、创收和收入切分、客户产品销售关系、员工机构归属、两融/保证金/息差。

详细报告：

```text
T0_ONTOLOGY_V2_REPORT.md
```

## 当前判断

第二版路线比单字段相似度更适合做业务 ontology 发现，尤其适用于字段命名大量使用词根、缩写，或者项目缺少正式指标沉淀的情况。

较稳妥的落地流程是：

```text
规则召回候选 -> 血缘证据验证 -> LLM 语义精炼 -> 业务专家确认 -> 正式纳入 ontology / 语义层
```

## 当前边界

- 自动发现结果默认 `knowledge_admission=needs_review`，不直接进入确定性问答。
- 跨表候选关系中低置信关系较多，需要按证据等级筛选。
- LLM 精炼覆盖范围取决于候选选择策略，当前更适合先处理高分候选和关键主题。
- 正式 ontology 的纳入仍需要业务专家确认，特别是概念边界、拆分粒度和同名不同义问题。
