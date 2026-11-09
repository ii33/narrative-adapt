# ADR-005 测试分四层

状态：已采纳

## 背景

全量 `pytest` 曾把 Qt/QML 页面、真实 SDK 往返、HTTP 与 MCP 传输混在一起：一次全跑既慢，又依赖桌面环境、网络与密钥，任一项不满足就红。于是改一处校验没人愿意跑测试，回归靠运气；真出问题时，也分不清是数据错了还是窗口没起来。

## 决策

测试按「回答什么问题」分四层，默认只跑第一层：默认（数据正确性，无标记）、`ui`（Qt/QML）、`native`（真实 SDK/HTTP/宿主）、`integration`（传输与管线）。

判据：默认套件只回答「数据是否被正确读写、推导和拒绝」；不产生数据错误的区域不进默认。`pyproject.toml` 的 `addopts = "-m 'not ui and not native and not integration' -n auto"`，非默认模块在收集阶段就跳过。

## 影响

- 代码：`tests/conftest.py` 是分层的唯一实现——`ui` 按 `_ui.py` 后缀与源码里是否出现 `PySide6` 判定，`native` 按 `NATIVE_HINTS` 子串，`integration` 按 `INTEGRATION_MODULES` 白名单，收集阶段直接 `pytest_ignore_collect`。
- 编写：新测试先问「算什么层」，纯函数优先；只有依赖持久化或修订号才建工程，且复制会话级 `project_template`，不每例建库。
- 运行：默认套件分钟级并行；UI 用 `pytest -m ui -n 4`，真实运行时用 `pytest -m native`，传输用 `pytest -m integration`；测试一律后台跑，日志落 `artifacts/logs/`。
- 提示词：提示词断言算数据正确性，留在默认套件，但只校验要点（与 ADR-004 配套）。

## 代价

- 默认套件绿不等于功能可用。UI 渲染、宿主进程、传输协议的问题默认不会被发现，只能靠显式运行与人工冒烟。
- 分层靠命名与白名单。新文件既不带 `_ui.py` 后缀、也不在 `INTEGRATION_MODULES` 里，就会掉进默认套件把它拖慢；反过来命名撞上某个 hint，会**静默不跑**——「这个用例怎么没执行」要回去读 `conftest.py`。
- 收集阶段读源码判断 `PySide6`：隐式规则让套件边界不直观，新贡献者容易误判自己写的用例属于哪层。
- 并行约束转移给测试作者：不得依赖执行顺序、模块级可变状态、固定端口、共享临时路径或固定 `sleep`。
- 非默认套件长期不跑会腐坏：约定是不删但保持可运行，实际第一次显式跑时往往先要修一轮。

## 另见

- `docs/TESTING.md`（分层表、编写新测试六条、维护规则）；`AGENTS.md`「测试分层与编写约定」
- `tests/conftest.py`、`pyproject.toml`（`[tool.pytest.ini_options]`）
