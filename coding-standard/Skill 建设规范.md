> 文档类型：建设规范
> 版本：v0.2
> 日期：2026-06-10
> 适用范围：课题小组、大数据 Skill 空间

---

**规范分层说明：**

- **课题（项目级）**：满足基本结构与评测要求即可入库。
- **企业内公开（企业级）**：在项目级基础上追加安全审查、更严格的评测与硬性指标。

---

## 2. 基本原则

### 2.1 一个 Skill 只做一类事，Description 是"路由入口"

- 一个 Skill 对应一类可重复的工作流程，不要做成"万能工具箱" [3, 4]。
- 系统在决定用哪个 Skill 时，只看 `name` 和 `description`（也叫 L1 元数据），不会预先加载 Skill 的完整内容 [1]。所以 **description 写得好不好，直接决定了 Skill 能不能被正确触发** [2, 3]。
- Description 必须说清楚两件事：**这个 Skill 是干什么的（WHAT）**，以及**什么情况下该用它（WHEN）**。用第三人称来写 [1, 3]。

### 2.2 只写模型不知道的东西

- 默认情况下，Agent 已经具备通用的知识和能力。Skill 只需要补充：领域特定的流程、组织内部的约定、工具/MCP 的用法、以及各种边界条件 [3]。
- 上下文窗口是共享资源，正文尽量精炼。详细的参考资料放到 `references/` 目录下，让系统按需加载（渐进披露）[1, 3]。

### 2.3 用评测驱动，以实测为准

- 先弄清楚"没有这个 Skill 时，Agent 在哪些场景下会出错"，再针对性地写出最少、最必要的指令 [3]。
- 迭代的依据是实际观察到的 Agent 行为，而不是作者的主观猜测 [3]。
- Description 的触发效果必须通过评测来验证 [2]；企业级还要额外验证触发后的执行质量 [4]。

### 2.4 核心内容保持通用，平台差异放到外面

- `SKILL.md` 的正文只写与具体平台无关的工作流程 [1]。
- 不同平台的差异（比如工具名称、路径变量、特定配置）写到可选的 `references/platform-adapters.md` 里，不要塞进核心正文。
- 环境依赖通过 `compatibility`、`metadata` 或 `allowed-tools`（实验性）来声明 [1]。

---

## 3. 基本要求

### 3.1 结构要求

| 项             | 要求                                                         |
| ------------- | ---------------------------------------------------------- |
| 目录            | 至少包含 `SKILL.md`；父目录名必须与 frontmatter 中的 `name` 一致           |
| `name`        | 必填；1–64 个字符；只能用小写字母、数字、连字符；不能以连字符开头或结尾；不允许连续两个连字符 `--`     |
| `description` | 必填；1–1024 个字符；不能为空；必须说明 Skill 做什么、何时使用                     |
| 正文            | YAML frontmatter + Markdown；**不限制正文的具体结构** [1]        |
| 可选目录          | `scripts/`（脚本）、`references/`（参考资料）、`assets/`（资源文件）[1] |

可选 frontmatter 字段：`license`、`compatibility`（不超过 500 字符）、`metadata`、`allowed-tools`（实验性）[1]。

### 3.2 内容要求

| 项 | 要求 | 依据 |
|---|---|---|
| Description | 包含 WHAT + WHEN；第三人称；包含用户可能使用的关键词 | [1, 3] |
| 术语 | 同一个 Skill 内部术语保持统一 | [3] |
| 文件引用 | 使用相对路径；建议从 `SKILL.md` 一层引用到 `references/` 或 `scripts/` | [1, 3] |
| 脚本（如果有） | 要么自包含，要么文档化所有依赖；必须包含明确的错误处理 | [1, 3] |
| 环境依赖（如果有） | 在 `compatibility` 或 `metadata` 中声明 | [1] |

### 3.3 触发评测

每个 Skill 必须提交 `evals/trigger.jsonl` 文件，用来验证 Skill 是否能被正确触发。

**数量要求**：至少 **4 条**

| 类型 | 数量 | 说明 |
|---|---|---|
| `should_trigger: true`（应该触发） | 2 条 | 其中 1 条是用户没有直接提到 Skill 相关领域、但确实需要该能力的场景 [2] |
| `should_trigger: false`（不应该触发） | 2 条 | 其中 1 条是 near-miss（和 Skill 共享关键词，但用户意图不同）[2] |

**格式示例**：

```jsonl
{"id":"t01","query":"查一下 horae 里 sparkIndex 上下游依赖","should_trigger":true}
{"id":"t02","query":"这个报表里的 margin 列能帮我算一下吗，文件在 ~/q4.xlsx","should_trigger":true}
{"id":"t03","query":"写一段 Python 读 CSV 用 matplotlib 画图","should_trigger":false}
{"id":"t04","query":"帮我改 Excel 里的公式，不是分析数据","should_trigger":false}
```

**通过标准（硬性）**：

- 每条 query 跑 **3 次**（因为大模型有随机性，单次结果不可靠）[2]。
- `should_trigger=true` 的条目：3 次中至少 **2 次** 成功触发 Skill（即加载了 `SKILL.md`）。
- `should_trigger=false` 的条目：3 次中最多 **1 次** 误触发。
- **4 条中至少 3 条** 满足上面的单条标准，整个 trigger eval 才算通过。

**怎么测**：在目标 Agent 环境里运行 query，通过执行日志或工具调用记录来确认 Skill 有没有被加载 [2]。

### 3.4 执行评测（建议）

建议额外提交 `evals/execution.jsonl`，至少 **2 条**，用来验证 Skill 被触发后的行为是否正常 [3, 4]：

```jsonl
{"id":"e01","query":"……","expected_behavior":["步骤1完成","调用了某 MCP","输出包含某字段"]}
```

项目级不强制要求数量门槛和通过率；但如果提交了，会作为评审时的参考。

---

## 4. 企业级追加要求

企业级 Skill 必须先满足第 3 节全部要求，再追加本节内容。
**本节不包含**：Skill 注册、版本发布、监控、职责分离等流程——这些由企业内部平台负责 [4]。

### 4.1 风险自声明（硬性）

在 `metadata` 和/或 `compatibility` 中明确声明以下信息 [1, 4]：

| 声明项 | 说明 |
|---|---|
| 是否包含可执行脚本 | `scripts/` 下有没有 `.py`、`.sh`、`.js` 等文件 |
| 是否调用 MCP | 列出 `ServerName:tool_name`；也可以用 `allowed-tools` 来声明 [1] |
| 是否访问生产数据 | 不访问 / 只读 / 可写 |
| 是否涉及网络访问 | 是/否；如果有，说明目标域名或 MCP |

如果 Skill 包含脚本、涉及生产数据或 MCP，必须先按企业风险指标完成安全审查才能入库 [4]。

### 4.2 安全审查（硬性）[4]

上线前必须完成以下审查（可以结合自动化规则 + 人工复核）：

| 风险指标[4] | 检查要点 | 风险等级 |
|---|---|---|
| 代码执行 | `scripts/` 里的脚本 | 高 |
| 指令操纵 | 是否包含忽略安全规则、隐藏操作、条件性改变行为等 | 高 |
| MCP 服务引用 | 是否访问了 Skill 本身范围之外的工具 | 高 |
| 网络访问 | URL、`fetch`、`curl`、`requests` 等 | 高 |
| 硬编码凭证 | API key、token、密码等 | 高 |
| 文件系统访问 | Skill 目录外的路径、宽泛的 glob 匹配、`../` 等 | 中 |
| 工具调用 | bash、文件操作及其他工具调用 | 中 |

**审查步骤[4]**：

1. 通读 Skill 目录里的全部内容（`SKILL.md`、references、scripts）。
2. 检查是否存在对抗性指令或数据外泄的模式。
3. 确认没有硬编码的凭证；所有凭证必须来自环境变量或企业凭据存储。
4. 如果包含脚本：在隔离环境中验证脚本的实际行为与声称的用途一致 [4]。

### 4.3 评测升级（硬性）

#### 4.3.1 触发评测

| 项      | 企业级要求                               | 依据     |
| ------ | ----------------------------------- | ------ |
| 数量     | 至少 **16 条**（应该触发和不应该触发大约各一半）        | [2, 4] |
| 负样本    | near-miss 的占比至少 **50%**             | [2]    |
| 每条运行次数 | **3 次**                             | [2]    |
| 多模型    | 在组织计划使用的每种主要模型上都跑一轮完整的 trigger eval | [3, 4] |

优化 Description 时，建议按约 6:4 的比例把评测数据拆成训练集和验证集，用验证集的通过率来选最优版本，避免过拟合 [2]。

#### 4.3.2 执行评测

| 项 | 企业级要求 | 依据 |
|---|---|---|
| 数量 | 至少 **5 条** | [4] |
| 内容 | 每条包含 `expected_behavior` 或结构化的 `expected_checks` | [3, 4] |
| 覆盖范围 | 正常路径、边界情况、关键步骤是否被跳过 | [4] |

需要读生产数据的 Skill：PR 准入阶段的 eval 应使用 mock/fixture 数据；真实生产环境的连通性验证由企业内部平台在发布环节完成，不在本规范定义。

#### 4.3.3 评测维度[4]

企业级准入必须覆盖以下四个维度：

| 维度[4] | 说明 |
|---|---|
| 触发准确性（Triggering accuracy） | 该触发时能触发，不该触发时不误触 |
| 独立自洽（Isolation behavior） | 单独使用时行为正确，不依赖目录外不存在的资源 |
| 指令遵循（Instruction following） | Agent 按 Skill 步骤执行，不跳过关键验证 |
| 输出质量（Output quality） | 产出结果正确、可用 |

### 4.4 企业级与项目级对照

| 项                 | 项目/开放级          | 企业级               |
| ----------------- | --------------- | ----------------- |
| 结构合规[1]     | 硬性              | 硬性                |
| trigger eval 数量   | 4 条（硬性）         | ≥16 条（硬性）         |
| trigger eval 通过标准 | 4 条中 ≥3 条达标（硬性） | 按 §4.3.3 维度评定（硬性） |
| execution eval    | 建议 2 条          | ≥5 条（硬性）          |
| 多模型测试             | 建议              | 硬性                |
| 安全审查              | 建议自检            | 硬性[4]        |
| 风险自声明             | 建议              | 硬性                |
| 治理/生命周期           | 不在本规范范围         | 由skillhub平台承接     |

## 4.5 案例
符合企业级的skill：

![[realtime-stock-cli.zip]]

不符合企业级的skill（待skillhub有更新功能后修正）：
![[realtime-stock-cli-0.3.0 (2).zip]]

---

## 5. 附录：eval 文件约定

### 5.1 `evals/trigger.jsonl`

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 唯一标识 |
| `query` | 是 | 模拟用户输入；建议包含路径、口语化表达、具体细节 [2] |
| `should_trigger` | 是 | `true`（应该触发）/ `false`（不应该触发） |
| `tags` | 否 | 可选标签，如 `explicit`（明确提及）、`near-miss`（近似但不该触发）、`implicit`（隐式需求） |

### 5.2 `evals/execution.jsonl`

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 唯一标识 |
| `query` | 是 | 触发 Skill 之后的任务 prompt |
| `expected_behavior` | 建议 | 用自然语言描述期望观察到的行为 [3] |
| `expected_checks` | 否 | 结构化断言，例如 `calls_mcp:horae:search_tasks` |
| `fixture` | 否 | mock 数据路径（企业级读生产数据的 Skill 建议使用） |

---

## 参考文献

[1] Agent Skills Specification（官方开放标准）. https://agentskills.io/specification

[2] Optimizing skill descriptions（触发优化指南）. https://agentskills.io/skill-creation/optimizing-descriptions

[3] Skill authoring best practices（写作最佳实践）. https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices

[4] Skills for enterprise（企业治理指南）. https://platform.claude.com/docs/en/agents-and-tools/agent-skills/enterprise
