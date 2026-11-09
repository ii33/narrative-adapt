# ADR-001 属性不存有效期

状态：已采纳

## 背景

早先的思路是给每条状态记录一对 `valid_from`／`valid_to`。这条路逼出两个无解的小问题：区间要由谁闭合——闭合早了，第 5 个事件点「他还是学徒」就查不出来；漏闭合，同一条事实就同时存在两条有效记录。改一次错还要连带改两处区间，历史因此变成可编辑的。

## 决策

属性只存事件处的变更，不存有效期。`property_definition` 只定形状，`property_change` 记 `set`／`add`／`remove`／`clear`／`unknown` 加 `event_id` 与 `sequence`（`M-07`）；某个事件点的状态由折叠推出（`M-10`），读工具用 `cut` 取截至该点的结果（`M-11`）。

可判定的判据只有一条：库里任何一条变更都没有起止时间字段，只有事件锚点；「某人在第 N 个事件点是什么身份」由同一套折叠逻辑给出唯一答案。

## 影响

- 代码：`property_core.reduce_changes()` 是唯一的折叠器；`property_store.state()` 每次查询现场折叠，`event_location.py` 的地点校验也复用它。
- 数据：schema 里没有 `valid_from`／`valid_to`／`expirations`。回滚是删掉边界之后的变更再重新推导，不是改区间。
- 读接口：`cut` 成为一等参数（G-16），页面与工具都要显式带阅读位置，否则读到的只是「最新」。
- 提示词：v23 明写「不写有效期」，模型不再被要求闭合区间，也就少了「猜终点」这类幻觉来源。
- 测试：断言折叠结果与冲突诊断（`STATE_CONFLICT`、`EVENT_ORDER`、`SEQUENCE`、`STATE_GROUP`），不需要构造时间区间。

## 代价

- 每次读取都要计算。`state()` 不缓存，且每次调用都重新取快照与变更记录（`temporal_state.state_at`）；属性视图 `node_properties()` 则是每个分组各折一次，一次要显示几十个属性就是几十次折叠。
- 没有落盘的状态表。「某事件点的全库世界状态」只能现算（`property_store.world_state(at)`），没有可直接 diff、备份或加索引的快照副本，大工程上是 O(变更数)。
- 查询必须带点。不带 `cut` 的答案只代表最新状态，回答历史问题要反复算，也不存在「两个时刻状态表直接相减」这种廉价操作。
- 变更记录只增不减（除更正与回滚），存储随事件数线性增长，没有按阅读位置压缩的机制。

## 另见

- `docs/MODEL.md` `M-07`、`M-10`、`M-11`；`docs/GLOSSARY.md` G-09、G-10、G-16
- `docs/data/PROPERTY_STATE_CONTRACT.md`；`src/narrative/property_core.py`、`src/narrative/property_store.py`
