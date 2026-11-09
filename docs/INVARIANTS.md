# 数据不变量清单

本表只收**程序真的会拒绝**的约束：每行的「校验点」是当前代码里的守卫函数，「诊断码」是它抛出的码，「覆盖测试」是断言它的用例。概念模型见 [MODEL.md](MODEL.md)（规则编号 `M-xx` 取自其第 2 节），术语见 [GLOSSARY.md](GLOSSARY.md)；没有对应 `M-xx` 的行属于 MODEL 第 3 节的硬约定。提示词要求但程序不校验的，另列在[仅提示词约束](#仅提示词约束未程序强制)一节，不混进本表。

判读方式：一条不变量成立与否只看「有没有违规数据能静默通过」。本表的行都要能指到具体函数与码，指不到的不写。

## 硬校验

| ID | 不变量（可判定的一句话） | 校验点（文件/函数） | 诊断码 | 覆盖测试 |
| --- | --- | --- | --- | --- |
| I-01 | 每个活动事件都必须在**自身事件处**记一条 `world` 地点状态：有效同语境 location 节点、显式「无」或「未知」 | `src/narrative/event_location.py::event_location_issues` | `EVENT_LOCATION_REQUIRED` | `tests/test_event_location.py` |
| I-02 | 地点状态只能锚定本事件，写在他事件处不能充当本事件地点 | 同上 | `EVENT_LOCATION_ANCHOR` | `tests/test_event_location.py` |
| I-03 | 有效地点值须是同语境、未撤回的 `location` 节点，或以 string/either 明确记录「无」「未知」；未知不能掩盖错值 | 同上 `_location_value_allowed` | `EVENT_LOCATION_VALUE` | `tests/test_event_location.py` |
| I-04 | 同一事件同一地点属性的变更互相冲突时报错，不按提交顺序挑一个 | 同上 | `EVENT_LOCATION_CONFLICT` | `tests/test_timeline_event_location.py` |
| I-05 | 每个活动物品必须有 `world` 持有状态（合法节点／「无」／「未知」）；失效或未记录不等于未知 | `src/narrative/item_possession.py::item_possession_issues` | `ITEM_HOLDER_REQUIRED` | `tests/test_item_possession.py` |
| I-06 | 持有者取值须为合法有效节点或显式「无」「未知」，认知 `holder_id` 不能顶替 | 同上 | `ITEM_HOLDER_VALUE` | `tests/test_item_possession.py` |
| I-07 | 同一事件位置的持有状态冲突不得择序 | 同上 | `ITEM_HOLDER_CONFLICT` | `tests/test_item_possession.py` |
| I-08 | 事件草稿必须带 `participants` 键；空数组表示「确无参与者」，缺键即拒 | `src/narrative/extraction.py::parse_nodes`（问题行 `path=["nodes",i,"participants"]`），`src/narrative/extraction_draft.py::submission_input_schema`（`allOf` 对 `type=event` 强制 required） | `EXTRACTION_CONTENT`，`DRAFT_SCHEMA` | `tests/test_extraction_diagnostic_paths.py`（参与者引用路径；缺键本身无专测） |
| I-09 | 容器（场景／大事件／主要故事）写入的属性若已由其下属事件按同组写过，该写入被丢弃并给 `info` 提示，事件记录胜出（`M-16`） | `src/narrative/property_core.py::container_property_duplicate`，`src/narrative/extraction.py::build_extraction` | `CONTAINER_PROPERTY_DEDUP` | `tests/test_container_property_dedup.py` |
| I-10 | 容器不落自身 `quotes`：容器证据只由已验证下级派生，提交里的容器引文被忽略 | `src/narrative/extraction.py::parse_nodes`（`is_container` 分支），`src/narrative/container_evidence.py::refresh_container_evidence` | 无错误码（静默丢弃） | `tests/test_extraction_container_evidence.py`，`tests/test_container_evidence_refresh.py` |
| I-11 | 进展属性只能落在直属子级：`storyline_progress` 记在 `big_event`，`big_event_progress` 记在 `scene` | `src/narrative/extraction.py::build_extraction`（`progress_owners`），`src/narrative/property_audit.py::audit_properties` | `progress_owner`（info） | `tests/test_storyline_contract.py` |
| I-12 | `world` 作用域不得带 `holder_id`；`claim`／`cognition` 必须带人物节点 `holder_id`（`M-08`） | `src/narrative/property_core.py::validate_records` | `SCOPE` | 无直接用例（同函数其余分支见 `tests/test_current_state_contract.py`） |
| I-13 | 引文必须**逐字**且**唯一**匹配原文；同一句多处出现时用合法 `disclosure.start` 消歧（`M-12`） | `src/narrative/property_core.py::_evidence_error`，`src/narrative/property_operations.py::record_change` | `EVIDENCE` | `tests/test_source_location_contract.py` |
| I-14 | `evidence` 只允许 `source_id` 与逐字 `quote` 两个键 | `src/narrative/property_core.py::_evidence_error` | `EVIDENCE` | `tests/test_current_state_contract.py` |
| I-15 | `disclosure` 须为合法原文 Unicode 坐标，且与引文起点逐字对齐 | `src/narrative/property_core.py::validate_records` | `DISCLOSURE` | `tests/test_project_check_locations.py` |
| I-16 | 节点证据引文须是原文子串，跨度须与原文位置相符（`M-12`） | `src/narrative/graph_service.py::precheck` | `EVIDENCE`，`EVIDENCE_SPAN` | `tests/test_project_check.py` |
| I-17 | 属性记录 ID 非空且唯一、`kind` 只能是 `property_definition`／`property_change`、字段走白名单 | `src/narrative/property_core.py::validate_records` | `ID`，`KIND`，`FORMAT` | `tests/test_current_state_contract.py` |
| I-18 | 变更必须锚定有效事件或动作，`sequence` 为非负整数，语境必须存在 | 同上 | `EVENT_REF`，`SEQUENCE`，`CONTEXT` | `tests/test_event_property_state.py` |
| I-19 | `clear`／`unknown` 不得携带 `value`；many 集合元素唯一、节点引用有效（`M-07`）。这是**写入**约束，不描述查询形状 | 同上 | `VALUE` | `tests/test_current_state_contract.py` |
| I-20 | 值类型与定义一致；`target_node_types` 只用于 `node` 定义；`symmetric` 为严格布尔；`one`／`many` 是**录入规则**（`one` 同一事件点只允许一个当前值，`many` 允许并存）与「标量关系按对方分别维护」（`M-09`），**不决定查询形状**——`state()`／`property_state()` 的 `known` 一律返回 `values` 列表，单值也是长度 1 的列表 | 同上 | `DEFINITION`，`VALUE` | `tests/test_property_symmetry.py`，`tests/test_state_values_contract.py` |
| I-21 | 五层直属关系：`scene→big_event→storyline`、`event→scene`、`action→event`，不跳层 | `src/narrative/extraction_draft.py::_draft_input_diagnostics`（`expected_parent` 映射），`src/narrative/storyline_ranges.py::analyze`，`src/narrative/graph_service.py::precheck` | `STORYLINE_CHILD_TYPE`，`COMPOSITE` | `tests/test_adaptation_hierarchy_contract.py` |
| I-22 | 每个活动事件必须经包含路径归属同语境的活动场景 | `src/narrative/containment_refs.py::event_scene_issues` | `EVENT_SCENE` | `tests/test_project_check.py` |
| I-23 | 动作必须直属恰好一个同语境活动事件，且不能含子项（`M-05`） | 同上 | `ACTION_EVENT`，`ACTION_LEAF` | `tests/test_granularity_actions.py` |
| I-24 | 包含关系不得成环 | 同上 | `CONTAINMENT_CYCLE` | `tests/test_project_check.py` |
| I-25 | 只有事件与动作能记参与者，且参与者须是人物／群体节点：地点用事件自身的地点属性、物品用持有者属性 | `src/narrative/graph_service.py::precheck`（存储写入），`src/narrative/extraction.py::parse_nodes`／`src/narrative/extraction_refs.py::References.resolve`（提取草稿） | `PARTICIPANTS` | `tests/test_item_possession.py`，`tests/test_extraction_diagnostic_paths.py` |
| I-26 | 只有事件记录 `predecessors`／`successors`，只能指向事件且不能指向自己 | 同上 | `EVENT_CHAIN` | `tests/test_current_state_contract.py` |
| I-27 | 正式资料版本必须等于提交携带的 `expected_revision`，否则整批拒绝 | `src/narrative/graph_service.py::precheck`，`src/narrative/graph_operations.py`（各写入口的 `expected_revision`） | `REVISION` | `tests/test_current_state_contract.py`，`tests/test_extraction_staging.py` |
| I-28 | 提取草稿未过预检时只返回 `staged` 与问题行，不写正式事实；过检即原子入库，不留半套写入 | `src/narrative/extraction_staging.py::submit`／`patch`／`_evaluate` | 状态 `staged`（无错误码） | `tests/test_extraction_staging.py` |
| I-29 | 回滚保留回滚点、删除其后的变更并清理引用，不重放节点旧版本 | `src/narrative/temporal_state.py::rollback`，`src/narrative/graph_operations.py::rollback_to` | 无错误码（`status=rejected` 时返回问题报告） | `tests/test_current_state_contract.py` |
| I-30 | 状态推导按 `(node_id, property_id, context_id, scope, holder_id)` 分组，不同组不得混算（`M-10`） | `src/narrative/property_core.py::change_group`／`reduce_changes` | `STATE_GROUP` | `tests/test_current_state_contract.py` |
| I-31 | 同一事件同一 `sequence` 的不相容变更报冲突，不按记录 ID 或提交顺序猜测（`M-10`） | `src/narrative/property_core.py::reduce_changes` | `STATE_CONFLICT` | `tests/test_event_property_state.py` |
| I-32 | 已登记的阅读顺序不可改写；`canon` 与故事时间轴标识不可修订 | `src/narrative/graph_service.py::precheck` | `SOURCE_ORDER_IMMUTABLE`，`INFRA_IMMUTABLE` | `tests/test_current_state_contract.py` |
| I-33 | 主要故事的完成边界须给出正式原文坐标并晚于所属成员起点，且成员不得跨出已完成区间；空候选批次整批拒绝（`M-04`） | `src/narrative/storyline_ranges.py::analyze`，`src/narrative/graph_service.py::precheck` | `STORYLINE_COMPLETION`，`STORYLINE_POSITION`，`STORYLINE_OVERLAP`，`EMPTY` | `tests/test_storyline_contract.py` |

## 仅提示词约束（未程序强制）

下面这些写在提取提示词（`src/narrative/prompt_defaults.py`，条目「提取通用要求」v23、「标准/精细提取粒度」v6）里，程序**没有**对应的拒绝路径：只能靠模型自检、真实模型验收与人工复核判断，不能当成已校验的保证。

| 约束 | 提示词出处 | 程序侧现状 |
| --- | --- | --- |
| 事件边界靠语义判断，不按动词、句子、数量或比例配额机械切分 | 「精细提取粒度」自检句 | 无校验：程序只查层级、引文与引用合法性 |
| 事件默认只提交一条足以证明其身份的代表性引文，不为起因过程结果分别罗列 | 「提取通用要求」 | 只要求引文非空且在范围内唯一，不限制条数上限 |
| 动作默认不提取，仅在用户明确要求时提取；不得把动作改标成事件 | 「标准提取粒度」「精细提取粒度」 | 无校验：`action` 与 `event` 走同一套结构校验 |
| 场景边界判据是「现场是否还连续」，地点只在事件上记，场景不按章节／切片／批次切 | 「提取通用要求」 | 只校验 `event→scene` 归属与同语境，不校验边界是否合理 |
| 次要人物、无名人物与具体地点须查全，不臆造身份、不拆重复实体 | 「精细提取粒度」 | 只校验参与者节点类型与引用有效性 |
| 主要故事的范围终点取下级证据派生的 `completion`，不以章节或批次结束代替语义闭合 | 「提取通用要求」 | 只校验 `completion` 不越出原文范围，不判断「是否真的完结」 |

改提示词时同步升该条 `version`，并按 [PROMPT_STYLE.md](PROMPT_STYLE.md) 用要点断言；本表「硬校验」一节的码是防漂移测试 `tests/test_doc_consistency.py` 的锚点，新增码要同时在代码与本表出现。
