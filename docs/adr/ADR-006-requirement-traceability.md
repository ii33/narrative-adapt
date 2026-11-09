# ADR-006 需求可追溯性：REQ 层与签名门禁

状态：已采纳（设计定稿，工具尚未实现）

## 背景

需求、文档、实现、测试之间没有机器可校验的链：需求只活在对话与文档散句里，`I-xx` 与用例之间没有人核对过「这条需求对应的验收标准是否还在」。这一轮的两次漂移就是证据——文档搬家把测试读取的路径搞断（靠新加的路径守卫才发现），旧口径在 `AGENTS.md`、数据模型文档与提示词三处并存（靠人工逐个改）。人工纪律不是不重要，但它不能作为唯一的保证。

## 决策

1. 新增 `REQ-<域>-<序号>` 层，按**功能**划分、数量可控，链成 `M-xx → I-xx → code(file::symbol) → test(file::case)`（设计稿 §2）。
2. 事实源是分片 YAML（`requirements/<domain>.yaml`）；Markdown 与 HTML 都是生成视图，手改产物由 `fmt --check` 拦下（§3、§9）。
3. `impl_version` 与 `test_version` 由工具按函数体 AST 规范化 hash 自动计数；REQ 版本只表达需求语义变化（§5）。
4. 一致性靠**签名 + 三条硬门禁**，不靠人手维护「版本号相等」：implemented 必须签名匹配、REQ 版本变而 verified 未跟进即红灯、planned/deferred 不得有锚点（§6）。
5. 变更入口统一为 `docops req change`；commit trailer 与直接改 YAML 是辅助通道，由 `check` 兜底（§7）。
6. 先在独立仓库 `narrative-docops` 并行开发，达到成熟度门槛（等价、回放、稳定、性能、可移植、可替换）再引入（§12）。

## 影响

- 主项目零新增：需求分片、生成物与缓存都在 `narrative-docops` 仓库（`requirements/`、`out/`、`.docops-cache.json`），主项目只多一步「`git archive` 快照 + `fmt --check` 校验副本」。
- 现有六项文档检查成为新工具的一部分：等价性达标后切换门禁，旧实现删掉即完成回滚。
- agent 侧多一个入口：默认只读，写入要 `--allow-agent-write` 且一次一条，`implemented`／`diverged` 必须人工签字。
- 既有契约版本不动：工具只读取 `GRAPH_SCHEMA_VERSION`、`DRAFT_VERSION` 与提示词 `version`。

## 代价

- 每条需求要人写 `intent` 与 `acceptance`，维护成本落在需求提出者身上；写歪的验收标准会让整条链「精确地错」。
- 重构会频繁触发 stale（改锚点函数即变黄），要靠 `refactor` trailer 加当次全绿测试消化，否则黄灯很快被无视。
- 独立仓库有双份维护成本；门槛若长期不达标，工具可能一直停在影子模式，投入见不到收益。
- 签名只能证明「有人复核过」，不能证明「实现正确」；它把「没人核对」变成红灯，但不替代真实模型验收与人工判断。

## 另见

- 设计稿：[docops-traceability.md](../features/docops-traceability.md)（§1–§14）
- 不变量与诊断码：[INVARIANTS.md](../INVARIANTS.md)
- 测试分层与默认套件边界：[AGENTS.md](../../AGENTS.md)「测试分层与编写约定」
