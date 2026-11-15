# 文档索引

这里给出两条阅读路径：一条给 agent（按编号与诊断码检索），一条给人（按概念、开发、决策顺序）。全部文档都以 [叙事模型](MODEL.md) 为概念事实源；文档与代码不一致时先改文档里的模型，再改代码。

编号四类，互不混用：

| 前缀 | 归属 | 含义 | 例子 |
| --- | --- | --- | --- |
| `M-xx` | [extraction/SPEC.md](extraction/SPEC.md) 第 4 节 | 模型规则：可判定的判断标准 | `M-07` 属性变更只写在事件处 |
| `E-xx` | [extraction/SPEC.md](extraction/SPEC.md) 第 3 节 | 提示词规范化出的硬约束：模型侧该怎么做（程序细节指到 `I-xx`） | `E-01` 未成功的操作不得声称已完成 |
| `I-xx` | [extraction/SPEC.md](extraction/SPEC.md) 第 5 节 | 不变量：必须成立的校验点，绑诊断码 | 事件地点必填 → `EVENT_LOCATION_REQUIRED` |
| `G-xx` | [GLOSSARY.md](GLOSSARY.md) | 概念词条：术语定义与易混对照 | 场景 S 与剧本场次 |

引用写法是「条目 + 结论」，例如「违反 `M-07`，返回 `DRAFT_SCHEMA`」「触发 `I-03`，码为 `EVENT_LOCATION_REQUIRED`」。

## agent 路径

按「规则 → 术语 → 不变量 → 操作」检索，编号稳定，可被引用。

| 顺序 | 文档 | 内容 | 检索方式 |
| --- | --- | --- | --- |
| 1 | [MODEL.md](MODEL.md) | 概念骨架（五层结构、属性与世界状态）+ 规则索引（`M-xx`、`I-xx`、诊断码 → SPEC 第几节） | 按 `M-xx`、诊断码、层名（T/B/S/E/A）`grep` |
| 2 | [extraction/SPEC.md](extraction/SPEC.md) | 提取任务的唯一规范：`M-01`…`M-16` 判断规则、硬约定、`I-01`…`I-33` 不变量与诊断码、属性状态合同与分类、数据流 | 按 `M-xx`、`I-xx`、诊断码反查触发条件与校验点 |
| 3 | [GLOSSARY.md](GLOSSARY.md) | 21 条术语（`G-xx`）与易混概念对照 | 按 `G-xx`、中文术语名；`M-xx` 集合须落在 SPEC 内 |
| 4 | [AGENT_GUIDE.md](AGENT_GUIDE.md) | 概念速览 → 事实源在哪（「概念／规则 → 权威来源」锚点表）→ 怎么验证 → 常见误改（9 条） | 按 `M-xx` / `G-xx` / `I-xx`、诊断码与工具名检索；常见误改可引 `M-14`（复核）、`M-15`（重复事件） |

用法：先把 [extraction/SPEC.md](extraction/SPEC.md) 的规则表与不变量表整段带进上下文，再按当前任务检索 `M-xx` / `I-xx` / `G-xx` 与诊断码；概念拿不准时查 [MODEL.md](MODEL.md) 第 1 节与该词条的 `G-xx`，别按字面自造同义词。

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
| 提取任务规范（`M-xx`／`E-xx`／`I-xx`、诊断码、属性合同） | [extraction/SPEC.md](extraction/SPEC.md) |
| 术语对照与易混概念（`G-xx`） | [GLOSSARY.md](GLOSSARY.md) |
| 概念速览、事实源锚点表、验证与常见误改（9 条） | [AGENT_GUIDE.md](AGENT_GUIDE.md) |
| 架构总览、环境与启动、数据流、常见任务与取舍 | [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) |
| 决策记录 | [adr/](adr/)：[ADR-001](adr/ADR-001-property-no-validity-window.md)、[ADR-002](adr/ADR-002-container-property-yields.md)、[ADR-003](adr/ADR-003-event-participants-required.md)、[ADR-004](adr/ADR-004-prompts-as-data.md)、[ADR-005](adr/ADR-005-test-layers.md) |
| 归档说明与历史材料 | [archive/README.md](archive/README.md)：[GRAPHRAG_DEPLOYMENT.md](archive/graphrag/GRAPHRAG_DEPLOYMENT.md)、[GRAPHRAG_IMPORT.md](archive/graphrag/GRAPHRAG_IMPORT.md)、[MS_GRAPHRAG_STANDARD.md](archive/graphrag/MS_GRAPHRAG_STANDARD.md) |
| 存储、包含关系、阅读位置 | [DATA_MODEL.md](data/DATA_MODEL.md) |
| 图数据模型 | [GRAPH_DATA_MODEL.md](data/GRAPH_DATA_MODEL.md) |
| 主张转写 | [MODEL_CLAIM_CONVERSION.md](data/MODEL_CLAIM_CONVERSION.md) |
| 结构查询 | [STRUCTURAL_QUERIES.md](data/STRUCTURAL_QUERIES.md) |
| 属性记录与状态推导、属性分类 | [extraction/SPEC.md](extraction/SPEC.md) 第 5 节（原 `data/PROPERTY_STATE_CONTRACT.md`、`data/PROPERTY_CATEGORIES.md`） |
| 属性分类验收矩阵 | [PROPERTY_ACCEPTANCE.md](data/PROPERTY_ACCEPTANCE.md) |
| 节点更正 | [NODE_CORRECTION.md](data/NODE_CORRECTION.md) |
| 工具参数与返回 | [AGENT_API.md](features/AGENT_API.md) |
| 公开仓库范围 | [PUBLIC_REPOSITORY.md](features/PUBLIC_REPOSITORY.md) |
| 上下文运行时 | [context-runtime.md](features/context-runtime.md) |
| 提示词写法（五段结构、硬指标） | [PROMPT_STYLE.md](PROMPT_STYLE.md) |
| 提示词版本与变更记录 | [extraction/PROMPT_CHANGELOG.md](extraction/PROMPT_CHANGELOG.md) |
| 提取提示词重写与真实验收 | [EXTRACTION_PROMPT_REWRITE.md](EXTRACTION_PROMPT_REWRITE.md) |
| 测试分层与默认套件边界 | [TESTING.md](TESTING.md) |
| 需求总表（agent 归纳，未经用户确认） | [REQUIREMENTS.md](requirements/REQUIREMENTS.md) |
| 剧情 / 剧本 / 分镜专题 | [SCREENPLAY_DATA_DESIGN.md](screenplay/SCREENPLAY_DATA_DESIGN.md)、[SCREENPLAY_PHASE_PLAN.md](screenplay/SCREENPLAY_PHASE_PLAN.md)、[PRODUCTION_DRAFT_DATA_DESIGN.md](screenplay/PRODUCTION_DRAFT_DATA_DESIGN.md)、[STORYBOARD_DIRECTOR.md](screenplay/STORYBOARD_DIRECTOR.md)、[STORYBOARD_TIMING.md](screenplay/STORYBOARD_TIMING.md)、[STORYBOARD_STYLE_PROVIDER.md](screenplay/STORYBOARD_STYLE_PROVIDER.md)、[STORYBOARD_VIDEO.md](screenplay/STORYBOARD_VIDEO.md) |
| 开发与验收约定（仓库根） | [AGENTS.md](../AGENTS.md) |
| 新会话交接（仓库根） | [PROJECT_HANDOFF.md](../PROJECT_HANDOFF.md) |

历史报告（`*_REPORT_*.md`、`ARCHITECTURE_AUDIT_*.md`、`V5_CONTRACT_AUDIT.md`）只用于核对当时结论，不作为当前事实来源；它们可能描述已被替换的旧口径。
