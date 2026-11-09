# 需求清单

本表是已确定需求的唯一事实源：每条需求一句话、可判定，并指回真实的代码、文档或产物。状态只有三种取值——`implemented`（已有实现与校验）、`partial`（部分达成，备注写明缺口）、`planned`（已定未做）。

表中「域」用代码 `EXT`（提取）／`PROP`（属性）／`SCR`（剧本）／`UI`（界面）／`ENG`（工程），与 Excel 版的「分类」列一一对应。

本表是全项目需求总览（人读）；`docops` 的 `requirements/*.yaml` 是可机器校验的子集，按需逐步回填，两者不要求逐条对应。

概念与规则编号以 [MODEL.md](../MODEL.md)（`M-xx`）与 [INVARIANTS.md](../INVARIANTS.md)（`I-xx`）为准；术语以 [GLOSSARY.md](../GLOSSARY.md)（`G-xx`）为准。证据锚点一律写「文件」或「文件::符号」，不写行号——行号会随改动失效。

| 域 | 编号 | 需求（一句话，可判定） | 状态 | 证据锚点 | 备注 |
| --- | --- | --- | --- | --- | --- |
| EXT | E1 | 原文按主要故事 T／大事件 B／场景 S／事件 E／动作 A 五层组织，层级为 T→B→S→E→A 且不跳级 | implemented | `docs/MODEL.md` 的 `M-01`–`M-04`；`src/narrative/prompt_defaults.py`（`extraction` v26）；`src/narrative/storyline_ranges.py::analyze` | T=卷/部、B=部内篇章、S=空间连续的连续事件、E=可独立概括、A=实施细节 |
| EXT | E2 | 主要故事 T 可在进行中创建并持续修订，条数不写死（一部一条或拆上下卷） | implemented | `docs/MODEL.md` 的 `M-04`；`src/narrative/prompt_defaults.py`（`extraction` v26） | 完成边界由程序从下级证据派生，不以章末代替语义闭合 |
| EXT | E3 | 场景 S 不承担地点身份，地点只记在事件上，上层人物与地点由成员派生 | implemented | `docs/MODEL.md` 的 `M-02`；`src/narrative/container_evidence.py::refresh_container_evidence` | 场景记地点会被静态检查当作层级重复 |
| EXT | E4 | 每个活动事件必须在自身事件处记一条 `world` 地点（节点引用／「无」／「未知」） | implemented | `src/narrative/event_location.py::event_location_issues`；`docs/INVARIANTS.md` 的 `I-01` | 缺失、清除、他事件锚定都报错 |
| EXT | E5 | 事件必须带 `participants`，取值限 `person`／`group`，空数组表示确无参与者或原文未交代 | implemented | `src/narrative/extraction.py::parse_nodes`；`src/narrative/graph_service.py::precheck`；`docs/INVARIANTS.md` 的 `I-25` | 地点用事件地点属性、物品用持有者属性，都不进参与者 |
| EXT | E6 | 每条属性变更必须给出分类（大类或显式「未分类」），程序不按属性名或取值猜 | implemented | `src/narrative/extraction_draft.py::_draft_input_diagnostics`；`src/narrative/prompt_defaults.py`（`extraction` v26） | 缺 `category` 直接拒收，不静默归位 |
| EXT | E7 | 认知/言说主张记在认知者或说话人（`holder`）身上，值优先用引用，未提及即留白，不与 `world` 混算 | implemented | `src/narrative/property_core.py::validate_records`；`docs/INVARIANTS.md` 的 `I-12`、`I-30`；`docs/GLOSSARY.md` 的 `G-36`–`G-39` | 同一属性换作用域或换持有人就是另一条记录 |
| EXT | E8 | 提取出的每条事实都能指回原文位置：逐字引文 + Unicode 区间 | implemented | `src/narrative/property_core.py::_evidence_error`；`docs/INVARIANTS.md` 的 `I-13`；`docs/MODEL.md` 的 `M-12` | 引文须逐字且唯一，多处出现要消歧 |
| EXT | E9 | 同一原文多次提取的事件边界稳定（三次运行的事件边界 Jaccard 均值 ≥ 0.8） | partial | `artifacts/extraction-consistency/v25/compare.json` | 证据取自 v25 的三次运行（`compare.json`：均值 0.844、run1↔run2 0.533、事件数 13/16/30）；v26 只改 `cardinality` 措辞、**未重跑验收**，边界稳定性结论沿用 v25 |
| PROP | P1 | 属性查询统一返回 `{kind, values[]}`，读取一律列出当前有效值 | implemented | `src/narrative/property_core.py::reduce_changes`；`src/narrative/property_store.py::state` | `kind` 取五态之一，`values` 为当前有效值数组 |
| PROP | P2 | 读取能区分五种状态：`known`／`unknown`／`absent`／`unrecorded`／`conflict` | implemented | `src/narrative/property_store.py::PROPERTY_STATE_CELL_KINDS`；`docs/GLOSSARY.md` 的 `G-40`–`G-44` | 「没记录」「明确不知道」「明确没有」含义不同，不能互相替代 |
| PROP | P3 | `cardinality` 只约束录入，不决定读取形状 | implemented | `src/narrative/prompt_defaults.py`（`extraction` v26）；`src/narrative/property_core.py::reduce_changes` | `one` 覆盖写、`many` 多项并存；读取一律列全部有效值 |
| PROP | P4 | 多位对方的关系仍是多条 `one` 记录，不因对方多而改 `many` | implemented | `docs/MODEL.md` 的 `M-09`；`docs/INVARIANTS.md` 的 `I-20`；`docs/GLOSSARY.md` 的 `G-34`、`G-35` | 郭靖多位师父＝多条记录，每条一个师父 |
| PROP | P5 | 属性不存有效期，状态按事件顺序与 `sequence` 折叠，读点用 `cut` 指定 | implemented | `src/narrative/property_core.py::reduce_changes`；`docs/INVARIANTS.md` 的 `I-30`、`I-31`；`docs/MODEL.md` 的 `M-10`、`M-11` | 同事件同序号不相容变更报冲突，不挑一个 |
| PROP | P6 | 描述性内容可多项并存：`many` 记同一事件点同时成立的多项（外貌要点、忌讳清单、随身物品） | implemented | `src/narrative/prompt_defaults.py`（`extraction` v26）；`src/narrative/property_categories.py` | 与 P4 相反的一侧：多项在同一记录内 |
| PROP | P7 | 容器写入的属性若与下属事件同组则让位：丢弃并回一条 `info` | implemented | `src/narrative/property_core.py::container_property_duplicate`；`docs/INVARIANTS.md` 的 `I-09`；`docs/MODEL.md` 的 `M-16` | `info` 附 `duplicate_of` 指向胜出的事件记录 |
| PROP | P8 | 分类词表末项固定为「未分类」，缺分类不静默归位 | implemented | `src/narrative/property_categories.py::UNCLASSIFIED` | 末项固定；缺省来源记为 `default_unclassified` |
| SCR | S1 | 剧本层一律写「场次」，提取层一律写「场景」，两者不混用 | implemented | `docs/GLOSSARY.md` 的 `G-19`、`G-04`；`docs/features/docops-traceability.md` | 「场次」＝`script_unit`，「场景」＝`scene` 节点 |
| SCR | S2 | 一个场次由场表字段 `plan` 与一串正文块 `block` 组成，可被镜头引用 | implemented | `src/narrative/screenplay_writing.py::submit_scene_table`；`src/narrative/screenplay_writing.py::submit_script_unit` | 场头信息在场表字段里，不在正文块重复 |
| SCR | S3 | 场由场景 S 与事件重组而来，可合并、拆分、重排 | implemented | `src/narrative/screenplay_scene_policy.py::prepare_generated_scene_table`；`src/narrative/screenplay_writing.py::apply_scene_table_changes` | 合并复用一个场次 ID，其余停用，正文保留 |
| SCR | S4 | 写作与修改只动显式目标，非目标场次与正文保持原样 | implemented | `src/narrative/screenplay_writing.py::validate_script_writing_tool_call` | 冻结目标清单之外的写入被拒 |
| UI | U1 | 深浅色跟随系统，也可显式覆盖为浅色或深色 | implemented | `src/narrative/appearance.py::is_dark`；`src/narrative/qml/Main.qml` | 外观模式存坏了回落到「跟随系统」，不拦启动 |
| UI | U2 | 页面按需创建；长原文按窗口加载，不一次渲染全文 | implemented | `src/narrative/qml/Main.qml`（`Loader.active`）；`src/narrative/qml/SourcePage.qml`（`chunkStart`／`chunkSize`） | 首次进入该页才实例化，之后保留 |
| UI | U3 | 静态检查的问题显示来源（节点／原文／记录）并可跳转定位 | implemented | `src/narrative/qml/StaticCheckPage.qml`；`src/narrative/native_bridge.py::resolveStaticIssue` | 跳转目标由问题自带的字段决定 |
| ENG | O1 | 提示词按原则优先撰写，且文本变更必须同时升版本（版本快照强制） | implemented | `tests/test_prompt_version_snapshot.py`；`docs/PROMPT_STYLE.md` | 只升版本不改文本、或改文本不升版本都红 |
| ENG | O2 | 文档体系分层：概念单一事实源、不变量表、开发手册、ADR | implemented | `docs/MODEL.md`；`docs/INVARIANTS.md`；`docs/DEVELOPER_GUIDE.md`；`docs/adr/` | 概念冲突时先改 MODEL，再改代码与提示词 |
| ENG | O3 | 文档一致性六项检查在默认套件里跑；运行产物按 7 天清理 | implemented | `tests/test_doc_consistency.py`；`scripts/prune_artifacts.py::DEFAULT_DAYS` | 链接、行号、符号、诊断码、术语、停用句式 |
| ENG | O4 | 需求可追溯：`REQ` 层 + 签名 + 三条硬门禁，产物为生成视图 | partial | `docs/features/docops-traceability.md`；`docs/features/docops-project-design.md` | M2（需求层）完成、M3（渲染）进行中；实现在同级的 `narrative-docops` 仓库，M4 切换未开始 |

## 状态统计

28 条：`implemented` 26 条、`partial` 2 条（E9、O4）、`planned` 0 条。

## 证据版本说明

- **E9 的证据版本落后于当前提示词版本**：验收数据取自 `extraction` v25 的三次运行，当前提示词是 v26（只改 `cardinality` 措辞、未重跑验收）。这是**已记录的差异**，不是数据可疑；结论沿用 v25，重跑后按新数据更新。
- 证据锚点里出现的 `artifacts/...` 路径是运行产物，不进版本库；清理后锚点会失效，需要时用同一脚本重跑取证。
