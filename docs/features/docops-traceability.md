# 文档操作体系：需求可追溯性设计

> 状态：设计设想，尚未实现（本文档不描述现有行为）

本设计要解决一个具体问题：需求、文档、实现、测试四者之间没有机器可校验的链。概念口径以 [MODEL.md](../MODEL.md)（`M-xx`）与 [INVARIANTS.md](../INVARIANTS.md)（`I-xx`）为准，本文只定义「怎么把需求钉到它们和代码上」，不重新定义概念。

## 1 目标与非目标

**目标**：给「用户需求 → 文档 → 实现 → 测试」建一条可校验的链。链上的每一段都有稳定编号，改任何一段都能算出受影响的需求条目并给出红／黄／绿结论。

**非目标**：

- 不做外部 issue tracker：条目存在仓库里，跟代码同一个历史。
- 不做语义自动判定：工具只比签名与锚点，**不判断实现是否正确**；是否正确由人工复核签字。
- 不替代现有检查：`M-xx`／`I-xx` 与诊断码是上游事实源，本体系只引用。
- 不追求覆盖全部代码：只覆盖有验收语义的功能，基础设施与重构不入库。

## 2 追溯链与粒度

一条链固定五段，方向只有一个：

```
REQ-<域>-<序号>  →  M-xx  →  I-xx  →  code(file::symbol)  →  test(file::case)
```

- `REQ-<域>-<序号>`：域是功能域（`EXT` 提取、`PROP` 属性、`SCR` 剧本、`SB` 分镜、`PROMPT` 提示词、`UI` 界面、`INFRA` 基础设施），序号三位起、单调递增、不回收。
- 粒度是**功能**，不是规则：一条 REQ 对应一个可判定的用户可见能力（例：「容器写入的属性若与下属事件同组则让位」）。总量预期几十到几百条，不是「一条 `M-xx` 一条 REQ」。
- `M-xx` 解释「为什么这条需求存在」，`I-xx` 给出「程序真的会拒绝什么」。**允许没有 `I-xx`**：纯语义或只靠提示词约束的需求（如「事件边界靠语义判断」）止步于 `M-xx`，并在条目里注明「无程序强制」。
- 一个 REQ 可挂多条 `M-xx`／`I-xx`／多个 code 锚点（一条需求可能落在多个工具上）；一个 `I-xx` 也可以被多条 REQ 引用。
- 反向也成立：任一 `I-xx` 若没有任何 REQ 引用，说明有不变量但没有需求来源，属报告里的「孤岛」项。

## 3 数据模型

条目按功能域分片存放：`requirements/<domain>.yaml`，每片 ≤200 条（见 §10 的拆分规则）。条目字段固定 17 个：

| 字段 | 含义 | 必填 |
| --- | --- | --- |
| `id` | `REQ-<域>-<序号>`，命令行分配 | 是 |
| `title` / `title_en` | 中文标题；英文标题可选（`--lang en` 用） | 中文必填 |
| `intent` | 为什么需要这条能力，一到两句；不含实现细节 | 是 |
| `acceptance` | 可判定的验收标准，数组，每条一句 | 见 §14 |
| `examples` | `positive` / `negative` 两组，各 0–2 条短例 | 否 |
| `status` | 六态之一（§4） | 是 |
| `origin` | `review`（逐条核对过）/ `backfill`（反补录）/ `user`（用户直提）/ `agent`（agent 起草待核） | 是 |
| `reviewed` | `by`／`at`／`notes`，人工复核记录 | implemented 必填 |
| `docs` | 相关文档路径数组（相对仓库根） | 否 |
| `model` | `M-xx` 数组 | 否 |
| `invariants` | `I-xx` 数组 | 否 |
| `code` | `文件.py::符号` 数组 | implemented／partial 必填 |
| `tests` | `tests/....py::用例` 数组 | implemented 必填 |
| `contracts` | 版本集合：`schema`／`draft`／`prompt: {key: version}` | 否 |
| `history` | 追加式变更记录：`at`／`kind`／`note`／`by` | 是 |
| `acknowledges` | 豁免登记：`gate`／`reason`／`expires`（§13） | 否 |
| `verified` | 上次人工核对通过的签名快照：`signature`／`impl_version`／`test_version`／`req_version`／`at`／`by` | implemented 必填 |

示例条目（字段值取自当前实现，工具每次运行现读，不手填）：

```yaml
id: REQ-PROP-003
title: 容器属性让位下属事件
title_en: Container property yields to descendant events
intent: 场景/大事件/主要故事与下属事件常表达同一事实，两层各留一条会把同一事实算两次。
acceptance:
  - 容器写入的属性若与某个下属事件同组（属性、语境、作用域、持有人在两侧相同），该写入被丢弃。
  - 被丢弃的写入回一条 info，并指向胜出的那条事件记录。
  - 容器上不与任何下属事件同组的同名属性照常保留。
examples:
  positive: ["场景写了「甲在场」，事件记了「甲在场」→ 场景那条被丢弃，duplicate_of 指向事件记录"]
  negative: ["场景写「甲在场」、事件写「乙在场」→ 两条都保留（不同主体）"]
status: implemented
origin: review
reviewed: {by: human, at: "2026-11-09", notes: "与 M-16 一致"}
docs: ["docs/MODEL.md"]
model: [M-16]
invariants: [I-09]
code: ["src/narrative/property_core.py::container_property_duplicate", "src/narrative/extraction.py::build_extraction"]
tests: ["tests/test_container_property_dedup.py::test_container_write_is_dropped"]
contracts: {schema: "11", draft: "conversation-draft/20"}
history:
  - {at: "2026-11-09", kind: backfill, note: "从 MODEL M-16 与既有测试反补录", by: human}
acknowledges: []
verified: {signature: "a1b2c3d4e5f6", impl_version: 1, test_version: 1, req_version: 1, at: "2026-11-09", by: human}
```

## 4 状态机

六个状态，转换只能沿下面这张表走；`stale` 不是状态。

| 状态 | 含义 | 允许的来源 → 去向 |
| --- | --- | --- |
| `planned` | 需求已定，还没有实现锚点 | new → partial／implemented／deferred／dropped |
| `implemented` | 验收标准全部有锚点，且签名与 `verified` 一致 | partial → ，或 stale 标记出现时降级 |
| `partial` | 部分验收标准有锚点，条目里必须列未覆盖项 | planned／implemented ↔ |
| `diverged` | 复核确认实现与验收标准**不一致**（人判断，不是工具判断） | implemented／partial → |
| `deferred` | 故意推迟，必须有 `history` 说明 | planned ↔ |
| `dropped` | 不再做，条目保留、ID 不回收 | 任意 →（终态） |

**`stale` 是 `implemented` 的失效标记**：实况签名与 `verified.signature` 不等时，条目对外报告为「implemented（stale）」；它不改变状态机，`req verify` 通过一次即清除。把 stale 做成状态会让状态机多出四条隐式边，是这类工具常见的过度设计。

状态谁能改：`planned/partial/deferred/dropped` 可由命令与 agent 改；`implemented` 与 `diverged` 必须带人工签字（`--by`，见 §11）。

## 5 版本通道

三条通道各管一件事，互不代签：

| 通道 | 何时 +1 | 由谁维护 |
| --- | --- | --- |
| `REQ version` | 仅当**验收语义**变化（`acceptance`／`intent` 改动）；措辞修正不动 | 人工（`req change --kind acceptance`） |
| `impl_version` | 锚点函数的**函数体 AST 规范化 hash** 变化一次记一次 | 工具自动（无需人工确认） |
| `test_version` | 锚点用例函数体的 AST 规范化 hash 变化 | 工具自动 |
| 契约版本 | 沿用现有：`GRAPH_SCHEMA_VERSION`、`DRAFT_VERSION`、提示词 `version` | 各自既有流程，本体系只读取 |

AST 规范化规则：解析为 AST 后丢弃 docstring、注释、空白与行号，按 `ast.dump` 稳定序列化再取 `sha256[:8]`。效果是「重排、改名局部变量、改注释不触发」，而「改控制流、改调用、改常量」一定触发。工程级提示词覆盖（`/project-N`）不进需求契约——它属于某个工程，不是产品行为。

## 6 一致性约束（核心）

实况签名是四元组的哈希：

```
signature = sha256(req.id + req.version + impl_hash + tests_hash + contracts_hash)[:12]
```

`verified.signature` 记的是「上次人工核对通过时的实况」。三条硬门禁：

1. **`implemented` 必须签名匹配**：实况 ≠ `verified` 即非法，报告降级为 stale 并亮红灯。
2. **`REQ version` 变而 `verified.req_version` 未跟进 = 红灯**：验收标准改严了却没人重新核对，比 stale 更严重——它意味着「验收」这个词已经不算数。
3. **`planned`／`deferred` 不得有锚点且签名必须为空**：有代码锚点的计划项会让影响面计算把未实现的东西算进依赖图。

三段示例：

- **绿**：`REQ-PROP-003` 的锚点、用例、契约都没动；实况签名等于 `verified.signature`；`acceptance` 未变。→ 通过。
- **黄**：重构把 `container_property_duplicate` 的函数体改了（`impl_version` 1→2），验收语义没变。→ 实况签名变化，条目报 `implemented (stale)`；跑 `docops req verify REQ-PROP-003 --by human`，确认行为未变后写入新签名，黄灯清除。
- **红**：把 `acceptance` 改严（`req_version` 1→2），但条目仍是 `implemented` 且 `verified.req_version` 还停在 1。→ 红：要么补一条实现，要么降为 `partial` 并列出未覆盖项。

## 7 变更入口

**主入口**（唯一改变语义的通道）：

```
docops req change <ID> --kind clarify|behavior|acceptance|status|deps --note "…"
```

每类门槛：

| kind | 门槛 |
| --- | --- |
| `clarify` | 不动 `version`；必须有 `--note`；不动签名输入 |
| `behavior` | 改 `code`／`tests` 锚点；同一次提交里须有对应改动，否则要求显式 `--allow-unanchored`；`version` 不动 |
| `acceptance` | 必须 `version`+1；清空 `verified.req_version`；若原状态是 `implemented` 直接降 `partial` |
| `status` | 只能走 §4 允许的边；→ `implemented` 必须签名匹配且带 `--by` |
| `deps` | 改 `model`／`invariants`／`contracts` 引用；触发反向查询，列出受影响条目 |

**辅助入口**（覆盖日常小改动，不承担语义变更）：

- **commit trailer**：提交信息里一行 `REQ-PROP-003: behavior - 说明`。它只用于把提交与条目关联、给 stale 条目自动补 `history`，不改变状态。
- **直接改 YAML**：允许；`docops check` 会按字段门槛兜底（缺 `--by`、`version` 未动、锚点与状态冲突等一律红灯）。本地编辑不被阻断，CI 负责拦。

另外两条命令：`docops req new --domain EXT --title …` 分配 ID；`docops req backfill --from docs|commits` 反补录历史条目（`origin: backfilled`）。

## 8 影响面计算

依赖图是双向的：`REQ → 锚点` 与 `锚点 → REQ`。一次变更新增/修改了哪些锚点，就能反查出所有受影响的条目。

- **全量**：遍历 `requirements/*.yaml` 建立索引，对每个锚点算 AST hash，与 `verified` 里的版本比较。
- **增量**（`--changed [base]`）：`git diff <base>..HEAD --name-only` → 变更文件 → 解析变更文件的 AST，只取函数体 hash 变了的符号 → 命中锚点即受影响。纯格式化与注释改动因 AST 不变而不进影响面。
- **契约变更**：`GRAPH_SCHEMA_VERSION`／`DRAFT_VERSION`／任一提示词 `version` 变了，命中声明了该契约的所有条目（不是全库）。
- **缓存**：`.docops-cache.json` 存 AST hash 与依赖图边，键是 `(文件, 符号, 规范化 hash)`。缓存**可随时删除**，删了只是重算；不进版本库。

## 9 输出

```
docops report --md|--html [--lang zh|en] [--changed [base]]
```

三份产物，全部是生成物：

| 产物 | 内容 |
| --- | --- |
| `docs/requirements/INDEX.md` | 摘要表：ID／标题／状态／版本／`M-xx`／`I-xx`／锚点数／上次复核 |
| `docs/requirements/DETAIL.md` | 核对视图：`intent`／`acceptance`／正反例／`code`／`tests`／复核状态／豁免 |
| `docs/requirements/index.html` | 单文件，内嵌 CSS，离线可看；内容同 DETAIL，带筛选与跳转 |

- **默认中文**；英文用 `--lang en`，读 `title_en` 等 `*_en` 字段，缺英文时回退中文并在行尾标 `(zh)`。
- 报告头固定一行：`generated by docops <ver> · commit <rev> · sha256:<输入集合摘要>`。这行让人知道「看到的是哪一版事实」。
- `docops fmt --check` 校验产物未被手改：重新生成后与文件逐字节比对，不一致即失败（手改产物 = 下次生成会覆盖，等于丢了改动）。
- 单条目的链路用 `docops trace <ID>` 打印（`REQ → M-xx → I-xx → code → tests`，即 §2 的顺序），也支持 `--json`；它只读，不改条目。

## 10 规模化

- **分片**：一域一片；单片 >200 条必须拆（域内分 `EXT-A`／`EXT-B` 子域）；`fmt` 检查片大小与片内 ID 域一致。
- **ID 分配**：只能由命令行分配，单调递增，`dropped` 也保留占位，**不回收**——回收会让历史报告与旧提交里的 ID 指向别的需求。
- **结构校验**：`schemas/requirement.schema.json` 用 JSON Schema 校验字段与枚举；`check` 先 schema、再语义（状态机、门槛、签名），两者报的码分开。
- **稳定序列化**：键序固定、两空格缩进、条目按 ID 排序；`req change` 只按文本区间重写**目标条目**，不重排整片——否则每次改动都会产生数百行 diff，评审必然失效。

## 11 agent 边界

- **默认只读**：`report`／`check`／`trace` 可跑；`req new`／`req change`／`req backfill`／`req verify` 需要 `--allow-agent-write`。
- **一次一条**：带写权限时也只接受单条 ID；多 ID 批量直接拒（批量改状态最容易被 agent 用来"顺手修绿"）。
- **签字不可代签**：`implemented`／`diverged` 与 `req verify` 都要 `--by` 人类标识。agent 可以起草条目、跑检查、给出建议，不能宣布"已核对"。
- **全命令 `--json`**：输出结构化结果（含 `ok`／`code`／`items`），错误也走 JSON，便于 agent 与 CI 直接消费；人读格式只用于终端。

## 12 引入方式

工具独立成仓库 `narrative-docops`，**只读主项目**：

- 不修改 `src/`、`docs/`（既有文档）、`tests/`、`pyproject.toml` 与 CI 配置；
- 只写 `requirements/`（独立仓库内）与主项目的产物目录 `docs/requirements/`、缓存 `.docops-cache.json`；
- 主项目通过薄包装调用（一行 invoke），删掉包装即回滚。

成熟度门槛（全部达标才准进门禁）：

| 门槛 | 判据 |
| --- | --- |
| 等价性 | 与现有六项文档检查零差异（同一批输入，结论逐条相同） |
| 历史回放 | 对近 10 次提交回放，命中已知漂移，误报 ≤2% |
| 稳定性 | 同一输入连续 10 次运行，产物逐字节一致 |
| 性能 | 全量 ≤5s；`--changed` ≤1s |
| 可移植 | 零第三方依赖，Windows 与 Linux 均通过；在第二个仓库跑通 |
| 可替换 | 薄包装 + 一次 revert 即可摘除，不留残留配置 |

三阶段推进：**影子模式**（只出报告，不进 CI）→ **门禁切换**（`check` 成为 required）→ **清理**（删除被替代的重复检查，docops 成唯一入口）。**不设时间计划**：门槛达标才进下一阶段。

## 13 代价与风险

- **人工维护成本**：`intent` 与 `acceptance` 必须人来写、人来核；工具能保证它们不漂，不能保证它们写得好。写歪的验收标准会让整条链「精确地错」。
- **stale 噪声**：任何改动锚点函数的重构都会让相关条目变黄。对策是 `refactor` trailer 触发自动 `req verify`，但要求**该次提交测试全绿**——红灯提交不自动核对，避免把"没跑测试"包装成"已核对"。
- **豁免机制**：`acknowledges` 需要 `reason` 与 `expires`（≤90 天），过期即失效并转为红灯；**三条硬门禁不可豁免**。豁免只用来压噪声（例如已知的、长期不改的 planned 条目缺文档链接）。
- **反补录占比**：`origin: backfilled` 的比例作为组织指标公示（占比高说明需求侧没有先启动）。只做指标，不做门禁——把指标设成门禁会催生"先写 REQ 再补实现"的形式主义。
- **双份维护**：独立仓库 + 主项目锚点，本质上维护两处事实；锚点写错时工具会报 stale，但"锚点写对了、语义没写对"需要人看。

## 14 未决项

1. **`acceptance` 是否允许为空**：当前决定——`implemented`／`diverged` 必须 ≥1 条，`planned` 可空。理由是空验收的标准等于没有标准。
2. **`diverged` 是否阻断**：当前只报告、不阻断。它需要人工判断，做成硬门禁会逼人为了过门禁去改状态。
3. **是否出覆盖率图表**：当前不做。图上墙之后会被当成 KPI，而"需求覆盖率高"与"实现正确"没有因果关系。

## 另见

- 概念与规则编号：[MODEL.md](../MODEL.md)、[INVARIANTS.md](../INVARIANTS.md)、[GLOSSARY.md](../GLOSSARY.md)
- 提示词版本通道：[PROMPT_STYLE.md](../PROMPT_STYLE.md)
- 测试分层与默认套件边界：[TESTING.md](../TESTING.md)
- 本设计的决策记录：`docs/adr/ADR-006-requirement-traceability.md`
