# ADR-003 事件参与者必填、不设状态位

状态：已采纳

## 背景

`participants` 是最常被下游读的字段：浏览视图要用它算场景人物，改编范围要按它取在场的人，写作上下文要据此判断本场有谁。允许省略时，图里分不清「这事件确实没有别人」和「模型忘了写」，下游只能靠猜或回原文重看。

## 决策

`type=event` 必须给出 `participants` 键，空数组表示「确无参与者或原文未交代」，不引入 `participants_status` 之类状态位。缺键整批返回 `EXTRACTION_CONTENT`。

判定：草案 schema 对 `type=event` 强制 `required: ["participants"]`（`extraction_draft.RESULT_SCHEMA` 的 `allOf`），入库前 `extraction.py` 再校验一次；两条路径的失败都让该批停在 `staged`。

## 影响

- 代码：缺键 → 该行进 `rejected` → `valid: False` → `staged`，整批不写库（`tests/test_extraction_identity.py` 的 `missing_participants_key` 用例锁住了这一行为）。
- 数据：空数组是合法值，并参与身份比对（`extraction.py` 的 `compare_participants` 对 event 一律比较），所以「有甲的同一事件」不会被误判成「空参与者」的同一次发生。
- 提示词：v23 与 schema 描述都写了「确无参与者或原文未交代时给空数组」，模型不需要猜默认值。
- 下游：场景人物集合、在场判断可以假定键存在，不必为空值兜底。

## 代价

- **无法区分「无参与者」与「未知」**。两种情况都只能是空数组，读者要看出来必须回原文；统计「多少事件缺参与者信息」时也只能人工判断。这是明确接受的取舍：要区分就得加状态位与一整套新校验，而眼下没有消费者需要这个区分。
- 一条易漏字段换来整批重提：漏写 `participants` 会让同批其他已正确的事实一起停在 `staged`，修补成本按批而不按条计。
- 取值域已收窄：`ALLOWED_PARTICIPANTS` 现为 `person`／`group`，`item`／`location` 参与者被拒绝并返回 `EXTRACTION_CONTENT`（草稿层 `extraction.py` 的参与者域校验、引用层 `extraction_refs.PARTICIPANT_TYPES`）；存储层 precheck 同步收窄（`graph_service.py` 对本次写入报 `PARTICIPANTS`，存量工程里遗留的非人物参与者仍可读出，下一次改写才被拒）。
- 收窄的理由：参与者语义是「参与事件的主体」，地点已有事件自身的地点属性、物品已有持有者属性，混进来等于把同一条事实写两遍。

## 另见

- 草稿版本 `conversation-draft/19` → `/20`（本次收窄）
- `docs/MODEL.md` 硬约定「参与者与持有者」；`docs/GLOSSARY.md` G-02、G-15
- `src/narrative/extraction_draft.py`（`RESULT_SCHEMA`）、`src/narrative/extraction.py`（`parse_nodes`、`ALLOWED_PARTICIPANTS`、`_participant_domain_error`）、`src/narrative/extraction_refs.py`（`PARTICIPANT_TYPES`）、`src/narrative/graph_service.py`（存储层 `PARTICIPANTS` 诊断）、`tests/test_extraction_identity.py`
