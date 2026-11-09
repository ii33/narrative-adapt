# AGENT 手册

面向：在本工程里读写叙事数据的模型／agent。
用法：先读第 1 节拿骨架，再按第 2 节定位事实源，改动后用第 3 节验证，动手前扫一遍第 4 节。

概念与判断规则以[叙事模型](MODEL.md)（`M-01`…`M-16`）与[术语表](GLOSSARY.md)（`G-01`…`G-21`）为唯一来源；程序真正会拒绝的约束见[数据不变量清单](INVARIANTS.md)（`I-01`…`I-33`）。本文件只做压缩与定位，不定义新概念。

## 1. 概念速览

1. 原文是唯一事实来源；引文必须逐字命中，定位与唯一性由程序校验。`M-12`、`I-13`
2. 同名或位置相近不构成「同一次发生」的依据；判重看原文证据。`M-15`
3. 归属五层：主要故事 T → 大事件 B → 场景 S → 事件 E → 动作 A。`M-01`–`M-05`、`G-02`–`G-06`、`I-21`
4. 跨层先后只看阅读位置（来源顺序 + Unicode 偏移）；章节、切片与批次只作索引。`M-06`、`G-01`
5. 场景不承担地点身份：地点是事件的 `world` 属性，上层地点由成员事件集合派生。`M-02`、`M-04`、`G-04`、`G-12`、`I-01`
6. 容器的证据、阅读起点与完成边界由程序从下级派生，模型只判边界、名称与直属关系。`M-04`、`G-12`、`I-10`、`I-33`
7. 属性变更只写在事件处：`set`／`add`／`remove`／`clear`／`unknown`，前三者带值，没有有效期。`M-07`、`G-09`、`I-19`
8. 状态是折叠结果而非最后一条记录；同一事件同 `sequence` 冲突必须诊断。`M-10`、`M-11`、`G-10`、`I-30`、`I-31`
9. 容器写入的属性若与某个下属事件同组，以事件记录为准，容器这条被丢弃并回一条 `info`。`M-16`、`G-11`、`I-09`
10. `staged` 只表示草稿暂存，`accepted` 才入库；`info` 不是必须清零的错误（MODEL §3）。`I-28`、`I-09`

## 2. 事实源在哪

改之前先确认这一行：同一规则只在一处定义，其余位置只能引用。

| 概念／规则 | 事实源 | 关键位置 | 不要改这里 |
| --- | --- | --- | --- |
| 五层语义与边界判据 `M-01`–`M-06`、`G-02`–`G-06` | `artifacts/prompt-drafts/extraction.md` | 正文 + 头部 `version:`；经 `scripts/rebuild_prompt_defaults.py` 生成 `DEFAULT_PROMPTS['extraction']` | `src/narrative/prompt_defaults.py`（生成物，见 §4.3） |
| 粒度两档 `G-21` | `artifacts/prompt-drafts/granularity_standard.md`、`granularity_fine.md` | 同上脚本；工程设置键 `extraction_granularity` | 在 `extraction.md` 里复制粒度细则 |
| 草稿字段与形状 `I-08` | `src/narrative/extraction_draft.py` | `RESULT_SCHEMA`、`_PROPERTY_DRAFT_SCHEMA`、`submission_input_schema()` | 在提示词里重列字段枚举 |
| 事件参与者必填 `I-08` | `src/narrative/extraction.py` | `parse_nodes()`：缺 `participants` → `EXTRACTION_CONTENT` | 新增「参与者未知」状态位 |
| 事件地点必填且值合法 `I-01`–`I-04` | `src/narrative/event_location.py` | `event_location_issues()`；`EVENT_LOCATION_REQUIRED`／`_ANCHOR`／`_VALUE`／`_CONFLICT` | 用场景地点或参与者代替 |
| 容器派生 `G-12`、`I-10` | `src/narrative/extraction_containers.py`、`src/narrative/container_evidence.py` | `derive()`（草稿期）、`refresh_container_evidence()`（入库期） | 让模型补写容器 `quotes` |
| 属性字段白名单 `I-17` | `src/narrative/property_core.py` | `CHANGE_FIELDS`、`DEFINITION_FIELDS`、`validate_records()` | 加 `valid_from`／`valid_to` |
| 状态推导 `M-10`、`G-10`、`I-30`、`I-31` | `src/narrative/property_core.py` | `change_group()`、`change_key()`、`reduce_changes()` | 另写一套分组或按记录 ID 排序 |
| 容器属性让位 `M-16`、`G-11`、`I-09` | `src/narrative/property_core.py` + `src/narrative/extraction.py` | `container_property_duplicate()`、`build_extraction()`；码 `CONTAINER_PROPERTY_DEDUP`（`info`） | 在 UI／CLI 再判一次 |
| 包含关系与五层硬校验 `I-21`–`I-25` | `src/narrative/containment_refs.py`、`src/narrative/graph_service.py` | `event_scene_issues()`、`storyline_issues()`；`GraphService.normalize()`／`precheck()`；`EVENT_SCENE`、`STORYLINE_*` | 在工具层判包含关系 |
| 暂存与入库 `I-27`、`I-28` | `src/narrative/extraction_staging.py` | `submit()`／`patch()`／`_evaluate()`；`CHECK_PENDING`、`FORMAL_REVISION_CHANGED` | 增加人工审批环节 |
| 原子写入与并发 `I-27` | `src/narrative/batch_write.py` | `prepare()`、`commit()`、`revision` 校验 | 直接写数据库 |
| 剧本层 `G-18`、`G-19` | `src/narrative/screenplay_*.py` | `screenplay_operations.py`（场次＝`script_unit`、场表＝`plan`、正文块＝`block`） | 把场次当提取层的场景（`G-04`） |
| 显示名与分类 | `src/narrative/property_labels.py`、`property_categories.py` | `LABELS`、`label_for()` | 把英文属性名当展示名 |
| 提示词写法与硬指标 | [提示词写作风格](PROMPT_STYLE.md) | 五段结构、字数上限、「不能」≤5、`version` 必升 | 在提示词里写实现细节 |
| 工具名与参数 | `src/narrative/tool_api.py` | `TOOLS` 注册表 | 让提示词与 `inputSchema` 各写一份枚举 |

## 3. 怎么验证

1. 只跑受影响的用例，且后台跑（不阻塞会话），见[测试约定](TESTING.md#运行方式)：

   ```powershell
   Start-Process .venv\Scripts\python.exe -ArgumentList '-m','pytest','-q','tests/test_<范围>.py','-n','0' `
     -RedirectStandardOutput artifacts\logs\pytest-<时间戳>.log -WindowStyle Hidden -PassThru
   ```

   默认 `addopts` 已排除 `ui`／`native`／`integration` 并开 `-n auto`；针对性用例加 `-n 0`，不要为一次改动跑全量。日志落 `artifacts/logs/`，用 tail 读末尾。

2. 静态检查必须过：`ruff check src tests`（[AGENTS.md](../AGENTS.md#每轮开发与测试收尾)第 1 条）。

3. 改提示词 = 改合同，四条同时满足（[提示词写作风格](PROMPT_STYLE.md#5-验收) §5、[修改流程](PROMPT_STYLE.md#6-修改流程) §6）：
   ① 只改 `artifacts/prompt-drafts/<key>.md` 再按脚本重建；② 该条 `version` 加一；
   ③ 断言改成要点（`pytest -k prompt` 全绿），不逐字锁整句；④ 行为类提示词另做真实模型多次对照，记录进 `docs/`。

4. 只改文档不触发真实模型请求；需要真实验收时用隔离工程与有界预算（[AGENTS.md](../AGENTS.md#每轮开发与测试收尾)第 2、3 条）。

5. 非法输入要断言 `staged`、问题码与字段路径，不用 mock 掩盖数据不变量（[测试约定](TESTING.md#编写新测试)第 4 条）。

流程约定没有编号（它们不是数据不变量），按章节锚点检索：

| 流程约定 | 权威来源 |
| --- | --- |
| 测试运行方式、分层与默认套件边界 | [TESTING.md#运行方式](TESTING.md#运行方式)、[#分层](TESTING.md#分层) |
| 提示词改法与升版本 | [PROMPT_STYLE.md#5-验收](PROMPT_STYLE.md#5-验收)、[#6-修改流程](PROMPT_STYLE.md#6-修改流程) |
| 收尾、提交与架构纪律 | [AGENTS.md#每轮开发与测试收尾](../AGENTS.md#每轮开发与测试收尾)、[#协作与架构](../AGENTS.md#协作与架构) |
| 新测试写法与维护（不 mock、不删测试） | [TESTING.md#编写新测试](TESTING.md#编写新测试)、[#维护](TESTING.md#维护) |
| 运行产物与历史代码边界（`data/`、`artifacts/`） | [AGENTS.md#运行产物与历史代码边界](../AGENTS.md#运行产物与历史代码边界) |

## 4. 常见误改

1. **把场景当地点容器。** 地点只记在事件的 `world` 属性上；场景只判「现场是否还连续」。`M-02`、`G-04`、`I-01`
2. **把剧本场次当提取层的场景。** 场次（`script_unit`）是可写作、可提交、可被镜头引用的演出单元，边界可以与场景不同。`G-19`
3. **手改 `src/narrative/prompt_defaults.py` 再跑重建。** 脚本以 HEAD 为基线做确定性重建（条目顺序与未改动条目都取自 HEAD），未提交的本地改动会被整表覆盖；正文只写在草稿文件里。（流程约定，见 §3 流程约定表「提示词改法与升版本」）
4. **改提示词不升 `version` 或不同步断言。** 提示词断言直接读 `DEFAULT_PROMPTS['<key>']['version']`，漏升即红（[提示词写作风格](PROMPT_STYLE.md#5-验收)）。
5. **`git add .` 冲掉 `.gitignore` 手写规则。** 本项目真实发生过：整目录暂存覆盖了手写忽略项，连 `.streamlit/secrets.toml` 的忽略也丢了，只能单独补回。加文件用显式路径，改完 `git status` 核一眼。（流程约定，见 §3 流程约定表「收尾、提交与架构纪律」）
6. **不查现状就重写文档。** `MODEL.md`／`GLOSSARY.md`／`INVARIANTS.md`／`data/DATA_MODEL.md`／`AGENTS.md` 已有当前口径，直接重写会退回旧说法；先 `grep` 现有措辞与编号再动手，`tests/test_doc_consistency.py` 会拦停用句式与断链。
7. **把上层容器已有的属性再写一遍。** 程序会丢弃这条写入并回 `info`（`CONTAINER_PROPERTY_DEDUP`，附 `duplicate_of`），不算失败；看到这条不要补写，也不要当 `error` 重试。`M-16`、`G-11`、`I-09`
8. **用 E/A 编号、章节或批次顺序判断先后。** 只看阅读位置；补录早先事实不会变成最新状态。`G-01`、`M-06`
9. **给属性加有效期。** 合同没有 `valid_from`／`valid_to`／`expirations`；状态变化用新的「事件处变更」表达，错录直接更正或删除。`M-07`、`G-09`、`I-19`
