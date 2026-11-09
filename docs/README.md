# 文档索引

这里给出两条阅读路径：一条给 agent（按编号与诊断码检索），一条给人（按概念、开发、决策顺序）。全部文档都以 [叙事模型](MODEL.md) 为概念事实源；文档与代码不一致时先改文档里的模型，再改代码。

编号三分，互不混用：

| 前缀 | 归属 | 含义 | 例子 |
| --- | --- | --- | --- |
| `M-xx` | [MODEL.md](MODEL.md) | 模型规则：可判定的判断标准 | `M-07` 属性变更只写在事件处 |
| `G-xx` | [GLOSSARY.md](GLOSSARY.md) | 概念词条：术语定义与易混对照 | 场景 S 与剧本场次 |
| `I-xx` | `INVARIANTS.md` | 不变量：必须成立的校验点，绑诊断码 | 事件地点必填 → `EVENT_LOCATION_REQUIRED` |

引用写法是「条目 + 结论」，例如「违反 `M-07`，返回 `DRAFT_SCHEMA`」「触发 `I-03`，码为 `EVENT_LOCATION_REQUIRED`」。

## agent 路径

按「规则 → 术语 → 不变量 → 操作」检索，编号稳定，可被引用。

| 顺序 | 文档 | 内容 | 检索方式 |
| --- | --- | --- | --- |
| 1 | [MODEL.md](MODEL.md) | 概念骨架 + `M-01`…`M-16` 判断规则 + 硬约定 | 按 `M-xx`、诊断码、层名（T/B/S/E/A）`grep` |
| 2 | [GLOSSARY.md](GLOSSARY.md) | 21 条术语（`G-xx`）与易混概念对照 | 按 `G-xx`、中文术语名；`M-xx` 集合须落在 MODEL 内 |
| 3 | [INVARIANTS.md](INVARIANTS.md) | 每条不变量（`I-xx`）对应的校验点与诊断码映射 | 按 `I-xx` 或诊断码反查触发条件 |
| 4 | [AGENT_GUIDE.md](AGENT_GUIDE.md) | 概念速览 → 事实源在哪（「概念／规则 → 权威来源」锚点表）→ 怎么验证 → 常见误改（9 条） | 按 `M-xx` / `G-xx` / `I-xx`、诊断码与工具名检索；常见误改可引 `M-14`（复核）、`M-15`（重复事件） |

用法：先把 `MODEL.md` 的规则表整段带进上下文（约 5k 字符，不会被截断），再按当前任务检索 `M-xx` / `G-xx` / `I-xx` 与诊断码。术语拿不准时先查 `G-xx`，别按字面自造同义词。

## 人类路径

按「怎么跑 → 概念怎么定义 → 为什么这么定」读。

| 顺序 | 文档 | 内容 |
| --- | --- | --- |
| 1 | [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | 架构总览 → 环境与启动 → 数据流（Mermaid）→ 常见任务 → 坑与取舍 |
| 2 | [MODEL.md](MODEL.md) 第 1 节 | 事件、属性、世界状态与五层结构 |
| 3 | [GLOSSARY.md](GLOSSARY.md) | 术语对照，用来消除「场景」「场次」这类同词不同义 |
| 4 | [adr/](adr/) | 关键决策的理由与当时的取舍（`ADR-001`…`ADR-005`） |

## 其它文档

| 主题 | 文档 |
| --- | --- |
| 术语对照与易混概念（`G-xx`） | [GLOSSARY.md](GLOSSARY.md) |
| 不变量与诊断码（`I-xx`） | [INVARIANTS.md](INVARIANTS.md) |
| 概念速览、事实源锚点表、验证与常见误改（9 条） | [AGENT_GUIDE.md](AGENT_GUIDE.md) |
| 架构总览、环境与启动、数据流、常见任务与取舍 | [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) |
| 决策记录 | [adr/](adr/)、[archive/README.md](archive/README.md)（历史材料与归档说明） |
| 存储、包含关系、阅读位置 | [DATA_MODEL.md](data/DATA_MODEL.md) |
| 属性记录与状态推导 | [PROPERTY_STATE_CONTRACT.md](data/PROPERTY_STATE_CONTRACT.md) |
| 工具参数与返回 | [AGENT_API.md](features/AGENT_API.md) |
| 提示词写法（五段结构、硬指标） | [PROMPT_STYLE.md](PROMPT_STYLE.md) |
| 提取提示词重写与真实验收 | [EXTRACTION_PROMPT_REWRITE.md](EXTRACTION_PROMPT_REWRITE.md) |
| 测试分层与默认套件边界 | [TESTING.md](TESTING.md) |
| 开发与验收约定（仓库根） | [AGENTS.md](../AGENTS.md) |
| 新会话交接（仓库根） | [PROJECT_HANDOFF.md](../PROJECT_HANDOFF.md) |
| 剧情 / 剧本 / 分镜专题 | [SCREENPLAY_DATA_DESIGN.md](screenplay/SCREENPLAY_DATA_DESIGN.md)、[STORYBOARD_DIRECTOR.md](screenplay/STORYBOARD_DIRECTOR.md)、[STORYBOARD_TIMING.md](screenplay/STORYBOARD_TIMING.md) |
| 属性分类与验收 | [PROPERTY_CATEGORIES.md](data/PROPERTY_CATEGORIES.md)、[PROPERTY_ACCEPTANCE.md](data/PROPERTY_ACCEPTANCE.md) |
| 节点更正 | [NODE_CORRECTION.md](data/NODE_CORRECTION.md) |

历史报告（`*_REPORT_*.md`、`ARCHITECTURE_AUDIT_*.md`、`V5_CONTRACT_AUDIT.md`）只用于核对当时结论，不作为当前事实来源；它们可能描述已被替换的旧口径。
