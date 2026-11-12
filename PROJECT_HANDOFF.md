# 交接：叙事数据工作台（narrative-adapt）

盘点：2026-11-11 · 分支 `feat/five-layer-and-appearance` · HEAD `b3d74ff`（已推送到 `github.com/ii33/narrative-adapt`）
后续（测试成本治理续作）：`df42a97`、`aaac87b`、`1190161` 三个提交**尚未推送**

> 上一版交接（2026-10-19）已过期：仓库根目录、测试通过数、架构描述都变了（它写的是 `C:\a\wk\narrative`、806 passed）。本文替换它。

---

## 1 这是什么

把原始叙事文本变成**可核对的结构化事实**，再用这些事实做改编：剧本、分镜。

三段主干：

| 段 | 做什么 | 边界 |
|---|---|---|
| **提取** | 原文 → 人物/地点/事件/场景/属性/关系，带原文证据 | 模型提案 → 程序定位引文与静态检查 → 通过即原子入库；模型原始输出与正式事实分开保存 |
| **检查** | 拿正式数据自查：引用完整性、时间顺序、属性状态一致性、持有者与地点覆盖 | 报"说做到了但事实没做到"；"该做还没做"算进度，不算矛盾 |
| **改编** | 五层结构 → 剧本场表 → 分镜资产与镜头 | 复用同一份叙事事实，不另造一套数据 |

技术栈：Python 3.13（`.venv`）、PySide6 6.12 + QML 桌面端、SQLite、`deepseek-harness-sdk`。许可证见 `REUSE.md`。

---

## 2 一分钟上手

```powershell
# 首次
.\setup-environment.ps1

# 起桌面端
.\start-desktop.ps1
# 或纯启动器
.\start.ps1

# 测试（开发期只跑默认门禁，别跑全量）
.venv\Scripts\python.exe -m pytest -q
```

- 工程数据在 `data/`（非可信运行产物，**不要从里面 import 代码**，见 `AGENTS.md`）。
- 模型配置走应用内的供应商设置；密钥不进版本库、不进验收报告。

---

## 3 架构要点

### 3.1 五层叙事结构

```
T 主要故事（一卷/一部） → B 大事件 → S 场景 → E 事件 → A 动作
```

- 场景 = 空间上接近、时间上连续的一组事件；地点记在事件上，场景不承担地点身份。
- 事件 = 有自身起因、推进、落点的情节单元；动作是事件的实施细节。
- 阅读顺序按原文坐标（文件序 + Unicode 位置），不按编号比较。

### 3.2 属性与状态（最容易出错的地方）

- 权威存储只有**当前定义**与**事件处 `property_change`**（`set` / `add` / `remove` / `clear` / `unknown`），状态是从这些记录**推导**出来的投影，不存"有效期"，不存撤回对象。
- 属性是**列表**：查询返回 `{kind, values: [...]}`。
- 四种状态：`known` / `unknown` / `absent` / `unrecorded`（+ `conflict`）。**空白不等于 unknown，也不等于 absent** —— `unknown` 是显式记录出来的。
- `cardinality`（`one` / `many`）是**录入规则**，不是"几个值"的描述。
- 认知类记录（`scope: cognition|claim`）记在持有者上：**不推断、不跟随 world 变化**；没记录就是 `unrecorded`。
- 每个活动事件必须有自己的地点状态（有效引用 / 显式"无" / 显式"未知"）；每个活动物品必须有明确的世界层持有者状态。缺失不算未知。

### 3.3 静态检查

入口 `Operations` → `project_check.check(ops)`，聚合四路：

```
graph_service.validate_snapshot   → 引用、包含、事件链、在场
temporal_state.change_issues      → 时间与顺序
property_core.validate_records    → 属性记录合法性
property_audit.audit_properties   → 物品持有、地点覆盖等覆盖类
```

输出统一带 `target.kind`，UI 据此跳转（8 种目标，未知回落工程级）。

### 3.4 对话、模板与工具

- 所有业务写入经 `Operations`；UI / CLI / MCP / 助手共用同一合同。
- **会话模板**（本轮新增）：内置 `提取`(39 工具) / `剧本`(53) / `分镜`(47)，提示词由 `prompt_defaults` 条目拼成，不另写文案。
  - 建会话时把 `system_prompt` 全文与工具名单**冻结**进会话元数据：之后改模板不影响已建会话，会话内工具数组不再变化。
  - 路由：原文页→提取、剧本页→剧本、分镜页→分镜；**用户手选的会话优先**，只有"有临时覆盖"或"还没有任何会话"时才走路由。
  - 自定义模板作用域：`app`（`~/.narrative/chat-templates.json`）与 `project`（工程 meta 表）；解析顺序 `project → app → 内置`；内置不可删、不可覆盖。

### 3.5 版本号与构建身份

**唯一真源是 `src/narrative/version.py`**；`pyproject.toml` 的 `version` 必须与它相等（`tests/test_version.py` 拦）。

| 对外串 | 例子 | 用在哪里 |
|---|---|---|
| `version_number()` | `0.2.0` | `pyproject.toml`、MCP `SERVER_VERSION`、`app.setApplicationVersion` |
| `tag()` | `v0.2.0-dev.islands` | 启动页底栏（`bridge.appVersionTag`） |
| `build_id()` | `20261112.ac8d5af` | 日期 + 7 位短哈希；dirty 追加 `.dirty`；读不到 git 时 `unknown` |
| `display_version()` | `v0.2.0-dev.islands+20261112.ac8d5af` | 状态栏 `appVersionLabel`、`bridge.appVersion`、启动日志 |
| `banner()` | `叙事图工作台 v0.2.0-dev.islands+20261112.ac8d5af` | `python -m narrative.cli --version` |

- 构建身份运行时从 git 读（`rev-parse HEAD`、`log -1 --format=%cd`、`status --porcelain`、`rev-parse --abbrev-ref HEAD`），**按进程缓存一次、不走网络**；没有 `.git`（装了 wheel）或 git 不可用时整体降级为 `unknown`，**不抛异常**。对外串的形状由 `tests/test_version.py` 的正则锁住：`v<主>.<次>.<修>-dev.<代号>+(<8 位日期>.<7 位哈希>[.dirty]|unknown)`。
- `dirty` = 工作区有未提交改动：版本串带 `.dirty`，banner 追加「（工作区有未提交改动）」。**带 `.dirty` 的截图不能当某条提交的验收证据。**
- 代号随 minor 换：`0.2` → `islands`（岛屿布局 / 窄窗响应式）。历史只记在这张表里。

**升版规则**

| 改了什么 | 动哪里 |
|---|---|
| 一次功能闭环落地 | 升 minor，换 `CODENAME` |
| 修 bug | 升 patch |
| 纯测试 / 文档 / 重构 | 不动版本 |

开发者约定：**改版本只改 `version.py`（必要时连 `CODENAME` 一起换），`pyproject.toml` 跟着改**；`tests/test_version.py` 拦下两边不一致，`tests/test_app_version_ui.py` 拦下界面没露出。

---

## 4 代码地图

`src/narrative/` 下有 150+ 模块，按职责记这几处就够定位：

| 关注点 | 文件 |
|---|---|
| 业务门面（唯一写入入口） | `operations.py`、`graph_operations.py`、`graph_service.py`、`property_operations.py` |
| 图与标识 | `graph_models.py`、`graph_store.py`、`node_identity.py`、`readable_ids.py`、`order_ids.py` |
| 属性与状态 | `property_core.py`、`property_store.py`、`property_audit.py`、`property_categories.py`、`item_possession.py`、`event_location.py`、`temporal_state.py` |
| 提取 | `extraction.py`（最大）、`extraction_draft.py`、`extraction_staging.py`、`extraction_refs.py`、`business_staging.py` |
| 检查 | `project_check.py`、`property_audit.py`、`temporal_state.py`、`containment_refs.py`、`container_evidence.py` |
| 对话与模型 | `assistant.py`、`assistant_host.py`、`harness.py`、`conversation_runtime.py`、`conversation_session.py`、`model_gateway.py` |
| 会话与模板 | `chat_store.py`、`chat_templates.py`、`usage_statistics.py` |
| 工具注册 | `tool_api.py`（62 KB，172 个注册项）、`tool_loading.py`（家族表 + 19 个核心常驻） |
| 剧本 | `screenplay_writing.py`、`screenplay_operations.py`、`screenplay_timeline.py`、`screenplay_compile.py` |
| 分镜 | `storyboard_operations.py`、`storyboard_assets.py`、`storyboard_panels.py`、`storyboard_camera.py`、`storyboard_video.py` |
| 桌面端 | `native_bridge.py`（127 KB，QML ↔ Python 的全部 slot）、`native_app.py`、`qml/*.qml`、`theme.py`、`appearance.py` |

UI 侧约定：所有颜色走主题令牌（`theme.py` 的 `TOKENS` / `LAYOUT`），`tests/test_theme_tokens.py` 会拦下不存在的令牌名与弹层里的硬编码颜色。

---

## 5 测试与验证

### 5.1 分层（默认只跑第一层）

| 命令 | 含什么 | 实测 |
|---|---|---|
| `pytest` | 数据正确性 + 合同；排除 `ui` / `native` / `integration` / `slow` | **979 条 / 约 41 秒** |
| `pytest --slow` | 再加 `slow`（storyboard 全部、host_text、project_copy_boundaries、单例 >8s） | **1149 passed / 1 skipped / 约 56 秒** |
| `pytest -m ui -n 4` | Qt Quick 界面 | 分批跑 |
| `pytest -m "integration"` / `-m "native"` | 非数据集成 / 真实 SDK | 需要环境 |

`slow` 的判据写在 `pyproject.toml` 的 marker 注释与 `docs/TESTING.md` 里：**单例 >8 秒、或 setup >50%、或涉及真实图像/子进程**。`tests/test_slow_marking.py` 六条元测试守着这条线，防止有人把贵用例塞进默认门禁。

### 5.2 已知偶发（`docs/TESTING.md` 有登记）

| 用例 | 条件 |
|---|---|
| `tests/test_project_changes.py::test_project_runtime_registers_and_dispatches_general_writes_only_in_active_task` | 并行 + 高负载；单跑 `-n 0` 通过 |
| `tests/test_project_copy_boundaries.py`（换不同用例挂） | `PermissionError [WinError 5]` 出现在 `duplicate_graph_project` 的目录改名；单跑该文件必过、重跑即过，**未改动的基线代码上也会出现** |
| `tests/test_chat_scroll.py` | 高负载下 `Transcript did not settle` 超时；单文件单跑通过 |
| `pytest -m ui -n 4` 的两条**存量红**（与布局改动无关，`HEAD` 上也红） | `tests/test_reading_position_picker.py`（懒加载使 relation 页 popup 不在，`[browse-…]` 那条）、`tests/test_event_merge_native.py`（需要真实模型服务，拿不到就 HTTP 500）。另有 `test_chat_scroll` / `test_storyboard_asset_elements_ui` / `test_lazy_pages_ui` / `test_static_check_ui` 等在 `-n 4` 下的并行 flake，串行全绿 |

另：并行下偶见 `ConnectionAbortedError [WinError 10053]` 栈打印，属服务端关连接噪声，无对应用例失败。

### 5.3 验证纪律（血泪换来的，别省）

1. **开发期不跑全量**：只跑你改动对应的文件（`pytest tests/test_x.py -n 0`）。全量留给推送前一次。
2. **不要同时开多个 worktree 跑测试**：会互相拖慢，还会制造 flaky。有一次全量因此从 55 秒涨到 774 秒，并逼出一条假失败。
3. **推送前才跑** `pytest --slow`（不是 `-m ""`，那个会连 ui/native/integration 一起跑）。
4. **`ruff check --no-cache src tests`**：工作区的 `.ruff_cache` 会给出过期的"All checks passed"，必须带 `--no-cache`。
5. 验收命令要在**干净检出**里跑一次，别只信工作区。

---

## 6 本轮（11-08 ~ 11-11）做完的事

| 提交 | 内容 |
|---|---|
| `3138d51` | 物品持有状态审计误报：`property_audit` 没把 `snapshot` 传给 `item_possession_issues`，候选集恒空 → 13 条全被误报。修后 44→31 条，`ITEM_HOLDER_REQUIRED` 13→0 |
| `349bdbc` | 「移除工程」对话框深色下白底：用了不存在的令牌 `t.color` → `t.card`；同时加主题令牌守卫测试 |
| `5badb94` | 原文页顶栏并成一行，标题与提示语删除，「提取全文/复核全文」改为「提取/复核」，点击插入待补全的范围草稿 |
| `b75f499`、`acf6c59`、`ce46d36` | docops 能力迁出：删设计文档与 ADR、需求表死链改指向独立仓库、登记已知偶发 |
| `4659f80` | **岛屿布局**：新增 `island_gap` / `island_radius`，外观设置里可切「紧凑 / 岛屿」，默认紧凑。紧凑模式经"反向还原 + 同尺寸渲染"逐像素比对，**changed_pixels = 0** |
| `098cb61`、`cfecb21`、`8919f1d` | **会话模板**：内置三个模板 + 创建即冻结 + 页面路由；助手可增删改自定义模板（app/project 作用域）；界面提供新建选模板、会话标记、「发往」提示与临时覆盖、「让助手编辑」草稿 |
| `31fcb7b` | 修测试写用户目录：两条用例没设 `NARRATIVE_CHAT_TEMPLATES_FILE`，14 个 worker 抢写 `~/.narrative/chat-templates.json`，表现为 `Different tests were collected` |
| `1812a1a` | `tools_hash` / `tools_count` 埋点：缓存回退可直接归因"是不是工具数组变了" |
| `3f6b7b8`、`024376f`、`43e75e1`、`b3d74ff` | 测试成本治理：本机 HTTP 超时 2s→60s、`slow` 分层与 `--slow` 入口、事件事实工程只建一次（该文件 60s→14s）、修跨模块 import 夹具的依赖丢失 |
| `df42a97`、`aaac87b` | **两条重夹具链改成「module 级模板 + 每例副本」**：`test_screenplay_writing.project` 与 `test_screenplay_source_references.prepared`（那二十来个 `from test_x import project` 的模块共用它们）。模板夹具放 `tests/conftest.py`——pytest 按「提出请求的模块」解析夹具参数，放测试模块里会报 `fixture '...' not found`。实测（`-n 0 --slow`）：剧本链 34 例 **62.4s→43.9s**、setup 合计 **34.2s→9.0s**；来源链 44 例 **58.7s→35.6s**、setup **41.5s→17.8s**；默认门禁 **50.4s→40.6s**。并行全量几乎不动（56.8s→55.5s）——并行墙钟由各 worker 的聚合吞吐决定，不是单个文件 |
| `1190161` | 测试文档：登记 `project_copy` 的 `WinError 5` 目录改名偶发（当天 6 次跑出现 2 次，未改动代码上也会出现），并写下模板夹具必须放 conftest 的约定 |
| `ac8d5af`、`4eb3843` | `project_copy` 发布阶段改名加有界重试（只吃 `PermissionError`，10/50/200ms，总预算 0.26s；实测 9 轮里 5 次拒绝，最坏连续 2 次、25ms 恢复） |
| `f36423a` | **岛屿布局窄窗响应式**：协作者窄窗改抽屉（不盖顶栏）、浏览页三栏自适应（分类栏收 44 窄条）、岛屿缝隙分层（页面内容区不再画卡片，`paper` 从缝里露出）。1127px 预览列 53→431px；新增 47 条 ui 用例 + 3 条令牌不等式守卫 |

### 缓存这件事的结论（别再重查）

- 实测命中率 **90.6%**，不是"低"；未命中集中在**每个任务的前几次请求**与**在途缓存回退**。
- 回退主因：`load_tools` 让 tools 数组在任务中途增长（tools 在 messages 之前，尾部追加 = 中部插入 → 其后全部重算）。基线样本里 12 次回退、作废 220k token，占全部未命中的 37.9%。
- 模板会话（工具集冻结）实测：**回退 12→0、作废 220k→0、`load_tools` 真实加载 0 次**。命中率 88.8%（短任务里 10.3k 的冷启动前缀锁死了上限，属结构不是缺陷）。
- 排查时先看 `model_requests[].tools_hash`：**哈希变了才是工具数组变了**；哈希不变仍回退，才去查 compaction 或会话重启。

---

## 7 未完成

1. ~~夹具作用域剩 4 个文件~~（已完成，见 `aaac87b`）：`test_screenplay_writing`、`test_storyboard_assets`（带 `sketch_style`）、`test_storyboard_camera`、`test_storyboard_camera_overlays` 已通过两条模板链受益。**仍有一处没动**：`test_project_copy_boundaries`（14 例、单文件 40~67 秒）。它的 `source` 夹具把源工程的绝对路径写进任务正文与 SDK 历史（断言 `copied_task.read_bytes() == raw_task`），改成"模板 + 副本"就得改动这些断言，按原约定放弃；并行下它也不是瓶颈（14 worker 里每例 setup 1.7 秒）。
2. ~~岛屿布局下窄窗口挤压~~（已完成，见 `f36423a`）：现状是"协作者在窄窗改成抽屉 + 浏览页三栏自适应 + 缝隙分层"。1127px + 协作者全开时预览列 53px→431px。契约全在 `theme.LAYOUT`（`window_min_width` / `workspace_min_width` / `assistant_dock_min_width` 1370 / `browse_*_min_width` / `browse_rail_width` 44），三条自洽不等式由 `tests/test_theme_tokens.py::layout_contract_problems` 守着；`tests/test_layout_responsive_ui.py` 覆盖 1000/1127/1360/1380/1500 × 紧凑/岛屿。**遗留**：次要文字 `muted` 在 `paper` 上对比度 3.81:1（改前 4.10，两档都低于 AA 4.5，属既有设计档位）；`screenplay` / `storyboard` 空态整页 `paper`、岛屿感弱于 browse/source（A 方案的固有取舍）；44px 窄条里的分类菜单点选只能人工探针（离屏抓不到 popup）。
3. **模板会话的真实缓存样本**：只跑过一次（副本工程、62 字短原文），长任务样本还没有。
4. **顶层概念文档**：`docs/CONCEPT.md` 已删。上一版是 agent 归纳的十条概念，用户指出"没有一条是我显式要求写入项目设计的"。现在等**用户自己**用"这是一个 xxx，能实现 ABCD，看起来是 xx 样子"的口吻写。
5. `data/` 里积压的运行产物可跑 `python scripts/prune_artifacts.py`（默认 dry-run）盘点。

---

## 8 踩过的坑

| 坑 | 现在的做法 |
|---|---|
| **行尾混排**：多文件是 CRLF/LF 混排，整体重写会变成"整文件改动" | 只用 `edit` 改；改完看 `git diff --stat`，别出现几百行的"重写"。本轮曾把 `test_relation_contract.py` 写成 1215 行空行污染，靠 `git checkout` 还原 |
| **`git add .`** | 禁止。逐文件 add；一个提交只放一件事（语义 + 断言同批） |
| **攒批推送** | 提交按主题逐条做，推送攒到一个功能闭环；推前三件事：`git log origin/..HEAD` 看清有无夹带 → 干净检出跑全量 → push（禁 `--force`） |
| **账本被覆盖** | 每个成员写自己的 `TASKS-<member>.md`；`TASKS.md` 归 Lead。`plan_init` 默认写 `TASKS.md`，成员必须显式传路径 |
| **会话串项目** | 这个仓库与另一个项目（`C:\a\wk\n\manual`）共处 `C:\a\wk\n`。**一个会话只写自己项目的仓库**；看到任务面板里出现别的项目名词，先确认再动手 |
| **缓存归因** | 别凭感觉；看 `tools_hash` |
| **夹具依赖跨模块**：`from test_x import some_fixture` 只带过来一个名字，它的参数按**提出请求的模块**解析 | 共享模板夹具放 `tests/conftest.py`（`writing_template`、`sources_template`）；非共享的依赖在被 import 的模块里一并显式 import（`facts_template`） |
| **`ruff` 假绿** | 一律 `--no-cache` |

---

## 9 文档索引

| 文件 | 看什么 |
|---|---|
| `AGENTS.md` | 开发与验收约定（最重要，细则都在里面） |
| `README.md` | 用户向说明与功能清单 |
| `docs/TESTING.md` | 测试分层、门禁/全量入口、已知偶发、标记判据 |
| `docs/INVARIANTS.md` | 数据不变量 |
| `docs/MODEL.md` | 数据模型 |
| `docs/GLOSSARY.md` | 术语（中文叫法与取舍） |
| `docs/PROMPT_STYLE.md` | 提示词写法（五段式、硬指标）；改提示词必须升 `version` |
| `docs/DEVELOPER_GUIDE.md`、`docs/AGENT_GUIDE.md` | 开发者与 agent 上手 |
| `docs/requirements/REQUIREMENTS.md` | 需求总表（**agent 归纳，未经用户确认，不构成项目定义**） |
| `docs/features/`、`docs/adr/` | 功能说明与决策记录 |
| `docs/archive/` | 历史报告，只作证据，不作当前结论 |

---

## 10 接手第一小时建议

1. 读本文 + `AGENTS.md` 的「测试分层」与「每轮收尾」两节；
2. `pytest -q` 跑一次默认门禁（约 1 分钟内），确认环境通；
3. `.\start-desktop.ps1` 起一次界面，点开一个工程，看原文页 → 提取 → 静态检查 → 剧本 → 分镜 这条链路；
4. 挑一个「未完成」里的条目开工，**开工前先确认它属于哪个项目、哪个会话**。
