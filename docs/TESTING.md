# 测试约定

自动化测试服务于两件事：守住数据正确性，并在真实模型验收前尽早暴露确定性错误。本文说明套件分层、默认边界和新测试的写法；验收流程见 [AGENTS.md](../AGENTS.md)。

## 分层

| 层 | 标记 | 内容 | 运行方式 |
|---|---|---|---|
| 默认（数据正确性） | 无 | 图与标识、属性定义与事件处变更、状态推导、证据锚定、层级与引用完整性、暂存与原子入库、修订与回滚、导入导出保真；提示词是数据，提示词断言按要点 | `pytest` |
| Qt UI | `ui` | QML 页面、交互、渲染、桌面启动 | `pytest -m ui -n 4` |
| 真实运行时 | `native` | 真实 SDK/HTTP、宿主进程、会话、安全隔离 | `pytest -m native` |
| 非数据集成 | `integration` | 传输与协议、运行时、媒体等重而偏管线/展示的用例 | `pytest -m integration` |
| 慢用例 | `slow` | 单例 >8s、setup 过半、真实图像/音频/文档 IO 或启动子进程的用例 | `pytest --slow`（默认排除） |

默认 `addopts` 排除 ui／native／integration／slow 四类标记并启用 `-n auto` 并行；标记与非数据模块清单在 `tests/conftest.py`。默认运行还会跳过这些模块的收集，避免为 PySide6/SDK 付导入成本。

**边界原则**：默认套件只回答“数据是否被正确读写、推导和拒绝”。UI、传输、日志、统计等不产生数据错误的区域不进默认套件，靠显式错误与人工冒烟判断。提示词却是数据的一部分：提示词断言留在默认套件，但只校验要点，不逐字锁整句。非默认测试不删除，保持可运行。

## 快速门禁与全量

日常提交只跑**门禁**，需要时再跑**全量**：

```
pytest            # 门禁：排除 ui / native / integration / slow
pytest --slow      # 全量：默认集 + slow（仍不含 ui / native / integration）
```

`pytest -m "not ui and not native and not integration"` 与 `--slow` 等价，留作显式写法。

**什么时候标 `slow`**（判据，避免随手标，也避免该标不标）：

- 单例耗时 **>8s**；或
- 该用例的 **setup 占自身耗时 >50%**（典型是每个用例重建工程/数据库）；或
- 涉及**真实图像、音频、文档编解码**，或要**启动子进程**；或
- 整文件平均 **>5s/例**且文件里没有可拆出来的纯函数用例（整模块标）。

**谁来保证不漏网**：`tests/test_slow_marking.py`。它是可判定的，不靠人记：

1. `slow` 必须注册在 `pyproject.toml` 的 `markers` 里；
2. 清单里的慢模块必须带模块级 `pytestmark = pytest.mark.slow`；
3. `test_storyboard_*.py` 一律算慢模块；
4. 默认集里导入重库（`subprocess`／`PIL`／`pypdf`／`reportlab`／`docx`／`mutagen`／`imageio`／`fontTools`／`wave`）的模块，必须标慢或在 `EXCEPTIONS` 登记理由；
5. `EXCEPTIONS` 不许腐坏：文件要存在、确实导入重库、理由非空、且没有被同时标慢。

新增重用例时，先问一句「它会不会把门禁拖慢 8 秒以上」；会就标 `slow`，否则第五条规则不会放过它。

**当前实测**（16 核本机）：门禁 **979 条 / 约 7 分 37 秒**；全量 `--slow` 1139 条 / 约 7 分 36 秒。两者接近，说明时间主要花在「每个用例自建工程 + 磁盘争用」上，不是少数慢用例——要压进 1 分钟得先做夹具作用域（见「维护」）。

## 运行方式

测试一律后台运行，不阻塞会话；单个用例、耗时的 `native` 套件和全量回归都同样处理——先起后台进程，再从日志取结果。

```powershell
Start-Process .venv\Scripts\python.exe -ArgumentList '-m','pytest','-q' `
  -RedirectStandardOutput artifacts\logs\pytest-<时间戳>.log -WindowStyle Hidden -PassThru
```

日志落 `artifacts/logs/`（`artifacts/` 已在 `.gitignore` 里，不进版本库），用 tail 读末尾结果；需要交互判断时先看日志再决定下一步，不要在前台守着跑。

## 编写新测试

1. **纯函数优先**。状态推导、校验、草稿归一化、编号与排序等，直接构造输入字典断言，不建工程。
2. **需要环境时才建**。只有断言依赖持久化、原子写入、修订号、真实文件布局或 `Operations` 合同时，才打开隔离工程。
3. **用模板复制，不重复建库**。会话级 `project_template` 已创建好空工程，每个用例复制一份即可保持“已知空状态”的隔离语义：

   ```python
   from agent_fixtures import fresh_project


   def test_something(tmp_path, monkeypatch, project_template):
       ops = fresh_project(tmp_path, monkeypatch, template=project_template)
       ...
   ```

   直接使用 `project` 夹具时同样走模板复制。除非测创建/复制本身，不要调用 `Operations.create_graph_project`。
4. **显式错误优先于 mock**。非法输入应断言返 `staged`、具体问题字段路径或明确异常；不要 mock 掉数据不变量校验来凑绿。
5. **并行安全**：默认套件多进程并行，测试不得依赖执行顺序、模块级可变状态、固定端口或共享临时路径，也不要用固定 sleep 等异步结果。
6. **提示词断言用要点**。关键词/短语组合，不逐字锁定整句；改提示词同步升 `version`，见 [PROMPT_STYLE.md](PROMPT_STYLE.md)。

## 已知偶发

- `tests/test_project_changes.py::test_project_runtime_registers_and_dispatches_general_writes_only_in_active_task`：
  `-n auto` 下偶发失败（一次全量里挂在“只有活动任务才接受通用写入”的断言上）。判据：单跑该用例必过、
  `pytest tests/test_project_changes.py -n 0` 整文件必过，只有并发跑才偶发——属测试隔离问题
  （共享状态或时序），不是产品缺陷。**暂不修**：修法要么加 `serial` 标记、要么找出被共享的状态，
  需要单独一轮排查。遇到它先按上面的判据复核，不要当成回归。

- 全量并发收集偶发 `Different tests were collected between gwN and gwM`（差异集合通常是某一个测试文件）：
  xdist 只有在**某个 worker 的收集阶段出错**时才会这么报，所以先按"import 期副作用"排查。已知的排查结论与复现条件：
  `src/narrative/chat_templates.py` 与 `tests/test_chat_templates.py` 在 import 期**没有任何 I/O**（模块顶层只有常量赋值，
  应用级模板文件是惰性读取，写只在保存时发生）；该文件曾有一个真实的并发隐患——用例把模板写进真实
  `~/.narrative/chat-templates.json`，14 个 worker 同时原子替换同一个用户文件会互相打架，现已改为每个用例
  `monkeypatch.setenv("NARRATIVE_CHAT_TEMPLATES_FILE", tmp_path/...)` 隔离。复现条件：让多个 worker 写同一个应用级配置文件。
  判据：`pytest --co -q -n 8` 连跑收集数一致、`pytest -q` 连跑不出现该消息，即为已消除；若再现，先查该轮是否有 worker
  报告过 `ERROR`/导入异常（`--tb=short -n 0` 复收集该文件），不要当成随机性。

- `tests/test_project_copy_boundaries.py` 曾偶发 `PermissionError: [WinError 5] 拒绝访问`，位置在
  `project_copy.py::duplicate_graph_project` 把暂存目录改名到最终路径那一步（`.project-copy-*` 下的数据目录 →
  目标数据目录）。实测同一天 6 次运行里出现 2 次，其中一次发生在**未改动的基线代码**上，两次挂的还不是同一条用例。
  判据：单跑该整文件必过、原地重跑即过，且失败点始终是 `os.rename` 的目录改名而非断言。看着像本机杀毒/索引
  还占着刚关闭的目录句柄（Windows 目录改名要求无任何打开句柄），不是路径语义或复制逻辑缺陷。
  **已加有界重试**：`_publish_rename` 只吃 `PermissionError`，退避档位 10/50/200ms（共 4 次尝试，总等待 0.26s）。
  实测依据：给 `Path.rename` 套记账插件跑该文件 9 轮，5 次拒绝分布在 4 轮（约 1/2 轮次命中），
  每次都靠下一次尝试恢复，最坏一次是连续 2 次拒绝、累计等待 25ms 后成功，涉及的目录是 `assistant`/`material`。
  判据仍保留：失败点若再次出现在这里、且是在 0.26s 预算之外仍被拒绝，说明不再是瞬时占用（例如真的被别的进程
  hold 住），按上面判据复核；别改断言，也别把退避档位无界放大
  （覆盖测试：`test_transient_rename_denial_during_publication_is_retried_and_publishes_the_copy` 与
  `test_rename_denial_after_the_retry_budget_propagates_and_leaves_no_partial_copy`）。

- `pytest -m ui -n 4` 下有两条**存量红**，与布局改动无关（在 `HEAD` 的另一棵 worktree 上同样红）：
  `tests/test_reading_position_picker.py` 的 `[browse-…]` 那条（懒加载让 relation 页的 popup 不存在，早于本轮），
  以及 `tests/test_event_merge_native.py`（需要真实模型服务，拿不到就 HTTP 500）。判据：两棵树逐条比对，
  交集才是存量红；`-n 4` 下还会看到 `test_chat_scroll`、`test_lazy_pages_ui`、`test_storyboard_*`、
  `test_static_check_ui` 等并行 flake，串行重跑即绿。

- UI 用例的滚动等待超时 `tests/test_chat_scroll.py` 抛 `AssertionError: Transcript did not settle: y=… height=…`：
  出现在**高负载/并行**的全量里（实测那一轮全量 774s，平时约 310s；同机器上另有任务在跑），等的是"内容滚到底"
  这个可观察条件，超时前 `y` 仍小于 `contentHeight`。判据：`pytest tests/test_moment_involvement.py -n 0` 必过、
  `-n auto` 单文件也过，只有全量高负载时才偶发——属时序/资源竞争，不是产品缺陷。复核步骤：先单跑该文件；
  全量再复现时看日志里的 `y/height` 是否只差一屏（说明是等待窗口不够，而不是渲染错位），必要时把等待上限调大，
  不要改成固定 sleep。


## 维护

- 新增非数据测试文件时，把模块名加入 `tests/conftest.py::INTEGRATION_MODULES`（或让其命中 `ui`/`native` 规则），默认收集会跳过；需要时仍可显式运行。
- 不通过删除既有测试来提速；先判断它属于哪一层，再决定是否移出默认。
- 测试产物放用例自己的 `tmp_path`；不要写入仓库或用户目录，也不要依赖上一次运行留下的数据。
- 默认套件保持分钟级（并行）；明显变慢时先查是否又引入了“每例建库”“真实等待”或未分层的重集成用例。
- 重夹具链一律「module 级模板 + 每例副本」：整条链（原文→提取→改编→剧本→时间线→选区→段落）按模块建一次，
  用例侧 `shutil.copytree` 到自己的 `tmp_path` 再 `Operations.open`（现成例子：`tests/conftest.py::writing_template`、
  `tests/conftest.py::sources_template`、`test_event_property_state.py::facts_template`）。实测整链重建约 1 秒，
  复制一份工程约 0.06 秒；副本沿用原工程名并落在 `tmp_path` 根下，用例里的路径断言不必改。
  **模板夹具放 `tests/conftest.py`**，不要放在定义它的测试模块里：pytest 按“提出请求的模块”解析夹具参数，
  放在测试模块里，那些 `from test_x import some_fixture` 的模块会报 `fixture '…' not found`。
- 排查「缓存命中率低」时，先看任务审计 `metadata-*.json` 的 `model_requests[].tools_hash` 与 `tools_count`：
  这两个字段是每条请求实际发出的工具数组的顺序指纹。**`tools_hash` 变了就说明工具数组变了**，其后所有 token 都要重算，
  缓存回退通常就发生在这一刻（`load_tools` 扩容、模板会话冻结名单与实际允许集合不一致等）；哈希不变却仍回退，才去看历史重写
  （compaction）或会话重启。
- 文档引用代码只写 `文件.py::符号`，不写 `文件.py:行号`：行号随改动失效，符号名由 `tests/test_doc_consistency.py` 校验存在。
