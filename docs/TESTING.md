# 测试约定

自动化测试服务于两件事：守住数据正确性，并在真实模型验收前尽早暴露确定性错误。本文说明套件分层、默认边界和新测试的写法；验收流程见 [AGENTS.md](../AGENTS.md)。

## 分层

| 层 | 标记 | 内容 | 运行方式 |
|---|---|---|---|
| 默认（数据正确性） | 无 | 图与标识、属性定义与事件处变更、状态推导、证据锚定、层级与引用完整性、暂存与原子入库、修订与回滚、导入导出保真；提示词是数据，提示词断言按要点 | `pytest` |
| Qt UI | `ui` | QML 页面、交互、渲染、桌面启动 | `pytest -m ui -n 4` |
| 真实运行时 | `native` | 真实 SDK/HTTP、宿主进程、会话、安全隔离 | `pytest -m native` |
| 非数据集成 | `integration` | 传输与协议、运行时、媒体等重而偏管线/展示的用例 | `pytest -m integration` |

默认 `addopts` 排除三类标记并启用 `-n auto` 并行；标记与非数据模块清单在 `tests/conftest.py`。默认运行还会跳过这些模块的收集，避免为 PySide6/SDK 付导入成本。

**边界原则**：默认套件只回答“数据是否被正确读写、推导和拒绝”。UI、传输、日志、统计等不产生数据错误的区域不进默认套件，靠显式错误与人工冒烟判断。提示词却是数据的一部分：提示词断言留在默认套件，但只校验要点，不逐字锁整句。非默认测试不删除，保持可运行。

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

## 维护

- 新增非数据测试文件时，把模块名加入 `tests/conftest.py::INTEGRATION_MODULES`（或让其命中 `ui`/`native` 规则），默认收集会跳过；需要时仍可显式运行。
- 不通过删除既有测试来提速；先判断它属于哪一层，再决定是否移出默认。
- 测试产物放用例自己的 `tmp_path`；不要写入仓库或用户目录，也不要依赖上一次运行留下的数据。
- 默认套件保持分钟级（并行）；明显变慢时先查是否又引入了“每例建库”“真实等待”或未分层的重集成用例。
