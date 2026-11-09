# ADR-002 容器属性让位下属事件

状态：已采纳

## 背景

场景、大事件、主要故事要表达的属性，经常正是下属事件已经说过的那件事：场景写「甲在场」，事件写「甲中途离开」。两层各留一条时，折叠按主体分组，上下分别推导，同一事实被算两次；两个容器的集合关系还会互相矛盾，而冲突诊断只会说「同事件同序号不相容」，指不出是层级重复。

## 决策

容器写入的某条属性若与某个下属事件同组（主体除外：属性、语境、作用域、持有人全同），以事件记录为准，容器写入被丢弃并回一条 `info`（`M-16`，诊断码 `CONTAINER_PROPERTY_DEDUP`，附 `duplicate_of`）。

实现：`property_core.container_property_duplicate()` 判定，`extraction.py` 在入库前过滤；判重发生在提交事务内，被丢弃的行不写库，也不改变该批的成败。

## 影响

- 代码：判重要遍历容器的后代事件（`container_evidence.descendant_events`），只在 `scene`／`big_event`／`storyline` 三类节点上生效（`CONTAINER_TYPES`）。
- 数据：容器上同名属性可以存在，但只在不与任何下属事件同组时保留；完成边界、人物与地点集合这类概括信息走派生（`M-04`），不再重复写一条记录。
- 提示词：v23 写明「同一属性已在下属事件存在时，写入按判重规则丢弃并给 info」，模型才不会把丢弃当失败反复重写。
- 测试：`tests/test_container_property_dedup.py` 覆盖丢弃、`duplicate_of` 指向、非容器不判重、层级不可解析时不判重。

## 代价

- **比较不含取值**：判重只看同组，不比 `value`。容器想对同一属性写一个「汇总值」（例如整场的状态）也会被丢弃，只能改用别的属性名（如 `storyline_progress`）。这是能力的净损失。
- 判断依赖层级可解析：容器没有可定位的下属事件、或包含关系缺失成环时，函数直接返回「不判重」，既不报错也不提示——此时重复会照常入库。
- 模型会反复写同一事实。丢弃是静默的（一条 `info`），看不到这条提示的调用方会以为写成功了，下一批再写一次。
- 真相分散：`info` 只说「已忽略」，事实实际落在哪个事件上要看 `duplicate_of`，人读报告时要多跳一次。

## 另见

- `docs/MODEL.md` `M-16`、`M-04`；`docs/GLOSSARY.md` G-07、G-11、G-12
- `src/narrative/property_core.py`（`container_property_duplicate`、`change_group`）、`src/narrative/extraction.py`、`src/narrative/container_evidence.py`
