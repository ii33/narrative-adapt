# 工程工具与统一对话

Qt 界面、CLI、MCP 和模型工具都通过 `Operations` 访问工程能力。工具名称、参数和读写标记以 `tool_api.describe()` 返回的当前工具表为准。当前数据库格式为 schema 11；旧版工程明确拒绝打开，不自动转换或修改原库。

## 统一对话

所有活动事件必须记录自身事件锚定的 `world` 地点属性。使用 `property="地点"`、`value_type="either"`，`value` 为地点节点引用或“无”“未知”；`op="unknown"` 时省略 `value`。`node` 和 `at_event` 均引用该事件。缺少地点的内容提交进入待修暂存，由原问题路径局部修补；程序不推测地点，也不自动补“未知”。改编和剧本新建事件可在同一次内容提交的 `changes.initial_location` 指定地点引用或这两个显式值。

草稿合同17增加 `event.location` 快捷输入：地点节点ID/`@n`引用、“无”或“未知”。程序复用该事件引文，生成world地点记录的主体、自事件锚点、ID与序号；原始模型草稿仍完整保存。此字段和同事件的显式地点 `properties` 不可双写，冲突暂存反馈，不自动选择；省略也不自动补值。

草稿合同18的默认指导是每个事件只提交一条代表性准确引文，支持事件身份与核心发生即可，不要求起因、经过、结果逐项引用，也不要求跨章事件在两侧各取引文。事件边界仍按完整情节判断，属性和关系仍各自提供必要证据。数组保留以支持明确的补证需求；不因多引文静态拒绝，不删除已有历史证据。

草稿合同19规定：场景、大事件和主要故事不由模型填写 `quotes` 或 `closing_quote`，只提交名称与直属层级关系（`children` 或下级父级引用）。程序从已验证下级逐层派生证据与阅读起点，沿用已有下级的真实来源和坐标；不要求把旧证据重新抄进本批范围。空容器、循环、错层和无有效下级证据按原始关系字段反馈，不能给上层补引文绕过。事件仍默认一条代表引文；动作、人物等实体以及属性关系继续提供自身必要证据。

模型可见工具合同不再暴露写作提交的 `task_id`、`manifest_ref`，由宿主按捕获任务注入；CLI/MCP与Operations合同保持完整，查询其他任务编号不会被覆盖。修订号和上下文依据仍按原读取结果校验，不自动取最新值。

完全访问模式提供 `run_host_process(program,args,stdin_text?)` 和 `run_host_script(language,script,stdin_text?)`。普通程序走argv直传，脚本文本由程序生成临时文件；默认工作目录为工程入口所在目录。命令仍使用原有command-N、取消、后台移交和审计，不增加命令总时限。输出编码歧义保留脱敏字节，可在 `read_host_command(encoding=...)` 重读，不重新执行。旧 `run_host_command` 仅用于确需shell语法的文本，Windows实际为cmd。

宿主 `read_host_file` 自动识别编码并报告BOM/换行，`write_host_file` 默认保留既有编码与换行；`edit_host_file` 精确匹配时统一换行，但不解释字面反斜杠。匹配缺失、不唯一、编码歧义或不能无损写回会明确失败。文本整读/编辑上限8MiB，不能截断后写回；二进制仍用base64。编码写出复用锁定版本的MIT agent-toolkit组件，来源见 `src/narrative/vendor/dsh_agentkit/PROVENANCE.md`，不依赖用户目录、不启用其代理/自动安装/远程配置。

### 常驻与按需工具

模型请求只携带常驻核心和当前聊天已加载的工具定义。完整工具仍在运行时注册，业务能力不按页面或创作阶段封闭；加载不改变 Operations 的校验、并发检查和审计。`list_available_tools` 默认返回业务分组简表，可按 `family`、`names` 或 `query` 查找简明工具说明；`load_tools(names=[...])` 或 `load_tools(families=[...])` 加载所需合同，下一次模型请求即可使用，无须重启进程或另开会话。不应预先加载所有组。

加载记录按聊天隔离，同一聊天在当前常驻进程内跨任务保留；关闭工程后重新按需加载。当前任务和访问模式始终约束实际权限，加载不增加授权，普通模式不能加载宿主文件和命令工具。CLI、MCP 的完整工具列表保持可用；无活跃模型加载上下文时，`load_tools` 作为只读合同查询返回所选定义，不修改业务数据或服务器注册表。

专用工作指南也按工具加载：提取工具附带当前工程的提取指南与标准/精细粒度约定，属性复核和事件合并工具分别附带对应指南。工程自定义提示仍生效；普通查询不再无条件携带提取、复核和合并全文。重复加载不重复附加已加载工具的指南，同一聊天继续使用原有 SDK 上下文。

开发验收在不验证会话恢复或缓存时，使用新建隔离工程与新会话，不以历史任务消息作为前置，也不预热或刻意破坏服务端缓存。总输入包含缓存命中部分；报告分别列总输入、已报告缓存命中、已知未缓存输入及输出，缺报不当作零。验收观察器发现确定的 SDK 工具输出合同错误时停止该验收任务并保存失败证据；这一开发观察规则不构成产品任务时限或调用次数上限。

协作者在当前聊天的同一个常驻模型进程中完成阅读、提取、自审和提交。任务没有硬性总时限，可手动停止；网络响应空闲检测不作为整项任务的时长上限。一次用户任务可以包含多次模型输出与工具调用，不预设提取轮和审核轮。模型可按需要查阅完整已导入原文和已有图数据。

提取、自审的目的，是从原文中识别节点、事件、属性和具体人物关系，核对重复、冲突以及旧属性是否仍然有效。失效判断由模型结合原文作出；程序负责引文、结构和预检等确定性校验。

模型每次公开输出的文本都按顺序进入当前聊天，包括工具调用之间的说明和最终回答；过程消息会保存，重开聊天后仍可查看。内部推理字段不作为聊天正文。

## 提取工具

| 工具 | 行为 |
| --- | --- |
| `preview_extraction` | 校验完整提取草稿并返回诊断，不写入图 |
| `submit_extraction` | 持久暂存已解析草稿并检查，合法立即原子入库；问题批返回暂存编号和具体问题行 |
| `list_extraction_submissions` | 找回本工程未解决暂存摘要，可按source_id筛选，分页不返回整稿 |
| `read_extraction_submission` | 默认只读取暂存状态、修订、问题；指定section和indices才读取相应条目 |
| `delete_extraction_submission` | 按暂存修订物理删除指定未入库草稿；即时原子命令，不修改正式事实或原任务历史 |
| `patch_extraction_submission` | 局部修补字段或追加缺失项，自动重检，通过即原子入库 |

`preview_extraction` 与 `submit_extraction` 使用相同参数：`source_id`、`result`，以及可选的 `start`、`end`。省略范围时使用完整原文。`result` 包含 `nodes`、事件变更 `properties` 及可选 `uncertainties`、`exclusions`；具体结构以工具 schema 为准。

原文位置使用 Python Unicode 字符坐标，范围为 `[start, end)`。模型直接提交的新引文必须逐字出自本次目标范围；读取其他原文帮助理解，不会扩大这类引文的范围。上层容器引用已入库下级时，程序沿用下级已经验证的精确证据，不要求重新落在本批范围。

暂存是程序校验修补工作区，不是待人工确认队列。合法结果通过 `Operations` 自动采用，模型存疑与程序诊断保留在结果和内部审计中。`status=staged` 表示尚未写入正式事实，不能冒充入库成功；若已经提交成功，而后续模型回答失败，已发生的入库动作仍须如实记录。

重启或任务恢复后，仅在需要找回未解决暂存时调用 `list_extraction_submissions(source_id=None, limit=20, after=0)`；只返回pending摘要和 `next_after`，不是每次提取的固定前置。

失败反馈包含 `submission_id`、`submission_revision`、`validation_status`、`problems`。`validation_status` 为 `pending/invalid/valid`，检查尚未完成时即使暂时没有error也不能声称通过。每个问题携带 `code/message/severity` 和 `targets`（`section/index/reference?/fields?`），能确定位置才标出，不伪造定位。反馈内容已足够时，直接用一次patch数组修多个问题，无需先read，也不再submit整批；只有缺少相关条目内容才局部read，不建立固定read→patch流程。`read_extraction_submission(submission_id)` 默认不重复返回整稿；必要时指定 `section="nodes"`、`indices=[2]` 读取原第三个节点。条目含 `index`、节点的原 `reference="@3"` 及 `item`。

`patch_extraction_submission(submission_id, expected_revision, patches, reason)` 的 `expected_revision` 必须是刚返回的 **暂存** `submission_revision`，不是工程 `revision`。`patches` 支持：

- `{"op":"set","section":"nodes","index":2,"field":"quotes","value":["修正后的准确引文"]}`：替换该问题字段。
- `{"op":"remove","section":"nodes","index":2,"field":"错误字段"}`：删除该字段。
- `{"op":"remove","path":["result_note"]}`：仅删除 `result` 内未知的顶层字段；不能删除 `nodes/properties/uncertainties/exclusions`，不能替换整稿或改变外层目标。
- `{"op":"append","section":"nodes","value":{"type":"person","name":"原文人物","quotes":["准确引文"]}}`：在末尾追加缺失项。

`section` 为 `nodes/properties/uncertainties/exclusions`，补丁 `index` 从0起，节点引用 `@n` 仍从1起。禁止删除整行、splice或重排，保证原批引用不漂移；不能借补丁修改原 `source_id/start/end`。每次局部修补自动完整重检，仍有问题继续 `staged` 并返回新修订和问题，通过即 `accepted/unchanged`、`submission_status=committed`，无需人工接受，也不要重新发送整批。已提交的暂存稿不可再patch；新的独立事实批次照常使用submit。工具参数JSON不合法时，入口拒绝该非法调用，继续按原工具纠错规则处理。

产品中没有独立的提取、属性、入库记录工作台，也没有对应的分轮提取、复核后提交或兼容转发接口。属性数据模型、阅读位置投影和自动入库所需的内部审计继续服务统一对话与数据浏览。

## 静态检查

`Operations.check_project()` 与 `check_project` 工具在一个只读事务内采样固定修订，统一检查结构、证据、引用、活动属性定义、事件归属及物品持有者等约束。已失效的历史记录仅作为引用背景，不误当成当前违规；运行不创建候选、不提升修订号。
返回 `checked_revision`、`current_revision`、严重级别计数和问题列表。问题携带可用的节点、属性记录及原文范围定位；无法唯一定位的引文不伪造高亮。左侧静态检查页可手动运行、筛选、查看详情和跳转，工程发生新修订时标明结果过期。

检查错误统一保留 `message/reason`（具体原因）、`path`（原始字段路径数组）、`location`（可读路径）、`location_scope` 和可用的 `target`。提取路径相对原始 `result`，例如 `["nodes",2,"quotes"]`；通用内容路径相对操作参数，不混入模型类型标签。引用问题用 `related_paths` 指向被引用条目的根因，不能用后续引用错误盖住原始缺失引文等问题。路径索引沿用原始输入，不按解析或排序后的结果重编号。全局约束明确标为整体提交并说明范围，不伪造具体修补字段；工程静态检查用对象或属性记录ID定位，保留原文来源和坐标。

## 通用工程读写

`get_schema` 返回图对象与属性记录合同；`read_project_state` 返回当前工程结构、事件属性变更及派生状态，供 agent 查漏补缺。
`preview_changes` 只做静态预检；`apply_changes` 使用同样的 `graph_objects`、`property_records`、`expected_revision`，另加写入依据 `reason`，通过后原子提交。
新增与修订节点、场景包含、上下文和时间轴可在同一批完成；属性状态变化在对应事件追加set/add/remove/clear/unknown记录；录入错误直接更正或删除。节点只保存当前对象，revision只用于并发检查。
过期修订、非法引用或包含环拒绝整批写入；每个活动事件最终须归属同语境活动场景。旧格式工程不迁移或写入；当前工程若已有孤立事件，可同批修复结构后再提交。
这些工具与属性、资料卡等现有业务写入工具都向统一 agent 开放，不依赖独立工作台或人工审核。

## 物品持有者

每个活动 `item` 必须有当前有效的世界事实层持有者记录，值是合法节点引用或显式“无”“未知”。未知必须明确记录，不能省略；`scope=cognition/claim` 的观点及其 `holder_id` 不替代世界事实层的物品持有者。“门派归属”“持有时间”等属性也不满足这条规则。
写入在完整拟写图与属性记录上校验；可同批新增物品、场景事件和持有者。换手时新增状态并保留历史，不允许仅撤掉最后一个有效持有状态而留下缺失。只读打开旧工程不伪造记录；界面的未记录或尚未披露提示不视为已入库的“未知”。

若旧“持有者”定义为 `node`，不能直接写入“无”或“未知”。可显式将当前定义改为 `either`，或经 `merge_properties` 物理合并到兼容定义；保留 `item_holder` 语义，直接重写引用，不建立别名链。

## 五层结构与名称

主要故事与大事件都只保存一个 `name`，直接使用清晰概括名；未完成也不使用“暂称”字段或名称后缀。`provisional_name` 不属于当前节点或提取合同，输入会被拒绝；`storyline`、`big_event` 名称尾部的 `（暂称）`、`(暂称)` 返回静态诊断供模型修正，不由宿主裁剪名称。

## 节点身份与重复提交

### 节点引用

已有节点优先使用实际正式 ID；程序分配的短 ID 带类型语义，例如 `P1` 为人物、`I1` 为物品、`E2` 为事件、`A1` 为动作、`S1` 为场景、`B1` 为大事件、`T1` 为主要故事。名称用于搜索和展示，不需要在每处引用中重抄。类型前缀不是父子关系或跨类型时间刻度，归属和先后仍由结构及原文披露位置表达。

同批新节点按完整 `result.nodes` 数组的位置使用从 **1** 开始的 `@1`、`@2` 等引用，允许前向引用。它们只在本次草稿内有效，程序解析后保存为正式 ID；不能拿上次草稿的 `@1` 当作工程 ID。节点声明只写一次名字，参与者、父级和属性等节点引用位置优先写 ID 或 `@序号`。

成功提交（`accepted` 或 `unchanged`）返回紧凑的 `node_ids`，例如 `{"@1":"P1","@2":"S1"}`，可供后续调用直接使用，不必再次查询编号；同身份重复槽可指向同一正式 ID。`preview` 不返回可供后续调用的正式编号，`nodes:[]` 不返回空映射。

已有节点不必重新列入 `nodes`、复制名称或重复提供旧引文；只新增属性时可直接引用其正式 ID。唯一名称仍可作便利输入，显式正式 ID 和 `@序号` 优先；名称匹配有多个候选时必须报歧义诊断，不能选择第一个匹配。

这些引用用于 `nodes.participants/scene/event/big_event/storyline/children`、`properties.node/holder/related_nodes/at_event`，以及 `value_type=node/either` 的节点值。`either` 的“无”“未知”保留字面值。属性变更中的节点选择、节点值及 `at_event` 使用同一规则；`existing_id` 必须是已存在的正式节点 ID，`context_id` 必须是现有语境 ID，不能写 `@序号`。

`name`、`quotes`、来源信息与 `string/any` 属性值是普通内容，不因字面上像 `@1` 或 `P1` 就改写。越界序号、引用缺失及类型或语境不符均返回诊断；解析前后排序、过滤不会改变 `@序号` 指向的原数组位置。


例如，假设工程已有乙 `P2`，以下草稿仅声明甲、大事件、场景和事件一次：

```json
{
  "nodes": [
    {"type":"person","name":"甲","quotes":["甲与乙握手。"]},
    {"type":"big_event","name":"双方会面","quotes":["甲与乙握手。"]},
    {"type":"scene","name":"会面","big_event":"@2","quotes":["甲与乙握手。"]},
    {"type":"event","name":"握手","scene":"@3","participants":["@1","P2"],"quotes":["甲与乙握手。"]}
  ],
  "properties": [
    {"node":"@1","property":"经历","op":"set","value":"与乙握手","at_event":"@4","quotes":["甲与乙握手。"]}
  ]
}
```


只为已有 `P1` 增加一条以已有 `E1` 为披露锚点的属性时，不必再次声明这两个节点：

```json
{"nodes":[],"properties":[{"node":"P1","property":"经历","op":"set","value":"与乙握手","at_event":"E1","quotes":["甲与乙握手。"]}]}
```

草稿节点可用 `existing_id` 引用当前工程已查证的节点，程序验证存在性、类型和语境；事件换了引文后仍可沿用原 ID，属性继续挂在同一事件上。`existing_id` 不与 `new_occurrence` 同时使用。

正式节点的 `evidence_spans` 保存所有逐字引文的 `source_id/start/end/quote`，预检核对字符位置及原文。主 `evidence/disclosure` 继续表示首个披露锚点，不因补证据而后移。章节标记 `source_marks` 不用来承载这些证据。

无显式 ID 时，自动复用要求同名或已知别名、同源、同场景与参与者，以及同一位置完整证据的唯一命中；同名、相近位置或宽泛段落重叠都不足以直接合并。无法唯一确认时，诊断把候选交给 agent 查证，不生成待人工审批队列。确是同名的另一次发生，可在原文依据支持下声明 `new_occurrence=true`。

事件引文在本次提交范围内出现多次时，必须先通过更完整的上下文或明确范围消歧；程序不能任取第一次出现的位置作为身份依据。

事件声明 `scene`、场景声明 `big_event`、大事件可声明 `storyline` 时，父节点引用可使用本批的 `@序号` 或当前工程正式 ID，无需为补充子事件重新提交父节点及其引文。已声明的包含关系必须解析并保存，现有父节点的子项保留后追加；缺失、歧义、类型或语境不符须返回诊断，不能静默丢掉关联。每个活动事件必须属于一个活动场景，哪怕该场景只有一个事件；可经事件包含路径归属场景。程序不按位置猜测或自动补造场景。

旧节点仍可凭原主证据精确命中复用。完整草稿哈希只解决相同提交的幂等，不能替代事件身份判断。程序不读取历史响应来偷偷补写旧证据。

复核还需检查已有重复事件。模型查阅两个事件与原文后，用 `preview_event_merge` 预检，再用 `merge_events` 显式合并；必须提供保留与并入的事件 ID、当前修订号、逐字原文位置和判断理由。同名本身不触发合并。 两个工具均使用 `source_event_id`（并入方）、`target_event_id`（保留方）、`expected_revision`、`evidence` 和 `reason`；`evidence` 为精确 `source_id/start/end/quote` 列表，须覆盖双方已有原文依据。该能力仅支持同一语境中的事件，不支持任意类型节点合并。

合并保留原节点、原属性记录及修订审计，以正式身份映射形成当前投影：旧事件退出活动列表，其属性与结构引用指向保留事件，证据合并保存。合并前的修订仍可重建当时两个节点；当前查询旧 ID 会明确告知合并去向。复核不能仅忽略重复节点后宣称已处理。

## 读取与原文组织

| 工具 | 用途 |
| --- | --- |
| `project_info`、`statistics` | 工程信息与统计 |
| `list_nodes`、`get_node`、`search` | 查找节点及详情 |
| `list_moments`、`scene_state`、`event_state` | 阅读位置及场景、事件状态 |
| `node_properties`、`property_categories` | 节点属性与显示分类 |
| `relation_map` | 人物及有方向的具体关系 |
| `list_sources`、`read_source`、`locate_source` | 原文清单、分段阅读与逐字定位 |
| `list_source_slices`、`read_source_slice` | 原文切片索引与完整切片内容 |
| `create_source_slices` | 批量创建浏览索引，保留完整原文 |
| `preview_pattern_slices` | 可选地试查标题模式，返回计数及首尾样例，不产生计划令牌 |
| `create_pattern_slices` | 按模式在一个程序事务中重算、校验并创建切片，无须抄hash |
| `node_material` | 节点资料卡与图片 |
| `audit_properties` | 属性结构诊断 |

支持 `cut` 的读工具用它表示阅读位置：省略时为最新，指定事件时只显示读到该处已披露且有效的事实。它不是强行把故事时间移到结尾。

规则章节先用少量原文样例确定标题模式，再让程序批处理，无需模型通读全文或逐章定位：

直接调用 `create_pattern_slices(pattern, start_index=0, limit=3)`，程序在同一事务内读原文、扫描、校验并创建；需要单独试查时才调用 `preview_pattern_slices`。两者都不要求模型传回hash或计划令牌。

`start_index` 是从零开始的标题匹配序号，区别于原文字符位置 `start`。范围结束取下一标题的起点；即使只选前三章，第三章仍以第四个标题为终点，只有原文最后一章才延至末尾。首个标题前的文字明确作为未纳入前缀返回，原文不删除。

没有稳定标题模式时，仍可读取内容后用 `create_source_slices` 批量提供明确范围：

```json
{"source_id":"原文编号","slices":[{"title":"第一章","start":0,"end":120}]}
```

字符位置不是 UTF-8 字节数或 UTF-16 单元数；可通过 `locate_source` 获取准确边界。切片只是索引，不代表已经提取，不改动原文或图中事实。完整已导入原文仍可按需要查阅。

同一关系性质可以指向多个对象；各对象分别保留事件变更序列。新增另一位朋友或下属不会自动使原对象失效。关系图与数据浏览都按事件顺序推导set/add/remove/clear/unknown记录的状态；物品持有者等非关系单值属性仍遵循原来的替代规则。

## 数据编辑

数据浏览和显式编辑工具仍支持节点修订、属性补录、更正、失效、分类整理以及资料卡维护。它们通过 `Operations` 校验并保存，不依赖已删除的工作台。更正直接修正错误记录并保留sequence；剧情失效追加remove或clear事件变更，阅读位置决定当前派生状态。

桌面协作者通过 `assistant.tool_catalog()` 获得当前工程全部业务写入工具，可以在同一任务中补建、修订节点与属性、调整包含结构及纠正记录。写入经 `Operations` 静态校验和原子提交，不以固定提取工具白名单或人工接受步骤限制查漏补缺。

## CLI 与 MCP

```powershell
narrative tool list
narrative tool call get_node '{"node_id":"P1"}' --project C:/projects/example.nproj
narrative nodes --project C:/projects/example.nproj --type person
narrative sources --project C:/projects/example.nproj
narrative-mcp --project C:/projects/example.nproj
```

CLI 的通用 `tool call` 与命名命令都使用同一工具注册表；未知或已删除工具直接报错，不转发到旧流程。长文本可用文件输入的命令，参数以各命令 `--help` 为准。

MCP 使用 stdio 上换行分隔的 JSON-RPC 2.0，支持 `initialize`、`tools/list`、`tools/call`、`ping`。指定 `--project` 后工具无需再传工程；否则工具参数须提供工程。

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"event_state","arguments":{"event_id":"E1"}}}
```

工具返回的 JSON 位于 `result.content[0].text`。工具执行失败使用 `isError: true`；协议错误使用 JSON-RPC `error`。

Python 调用使用相同入口：

```python
from narrative import tool_api

result = tool_api.call("event_state", {"event_id": "E1"}, project="C:/projects/example.nproj")
```

原始响应、usage、终止原因及校验诊断保存在任务审计中。历史任务快照与报告用于核对当时结果，不作为当前代码或工具合同。

## 提取粒度与按需动作

`Operations.load_project_settings()` / `save_project_settings({extraction_granularity, profile})` 读取和保存本工程的粒度与模型设置。粒度为 `standard`（阅读梳理）或 `fine`（默认精细，细查独立叙事转折和证据）。每个统一助手任务读取当时的设置，改变粒度不自动重提旧数据；只改粒度不固化原本继承的全局模型配置。

事件的 `participants` 引用原文明示出场或实际起作用的人物；有原文支持的事件地点用属性 `地点` 引用 `location` 节点（`value_type=node`、`scope=world`、`at_event` 指向该事件，quotes 提供原文证据）。地点不可证时不猜，上层容器通过成员派生，无需重复抄写。精细档额外细查非关键、无名但原文可区分的人物和具体地点，不臆造姓名/身份，也不拆分无法区分的泛称。

事件介于场景与动作之间：场景组织连续时空与语境，事件记录其中可独立概括的叙事进展，动作是事件的实施细节。同一场景、同一总体目标可有多个各有起因、推进与落点的事件，不要求另起目标或后续独立展开才拆分；也不按每个发现、决定、动词或属性变化自动切界。fine保留有独立意义的过程、转折与信息节点，兼查过细的动作碎片和过粗的整场概括。边界由模型结合原文语义判断，不设事件数量、场景比例、题材词表或语义静态拒绝。

标准、精细都默认不创建动作。明确请求动作提取时，草稿可使用 `type: action`，以 `event` 优先引用直属事件的正式 ID 或本批 `@序号`（唯一名称可作便利输入），并给出逐字原文引文。动作必须有且只有一个同语境父事件，不能直接归属场景或含子项。已有动作经查证后用 `existing_id` 复用。节点编辑器的“动作提取”按钮将明确父事件 ID 发送到当前统一对话，不另起专用进程。

四级阅读位置为故事线、场景、事件、动作；节点编号 A/E 仅表达各类身份，跨类型顺序按原文来源与披露坐标计算。

## 简短编号与模型工具输入输出

原文SRCn、属性变更PCn、切片SRCn-SLn由程序分配，不要求模型编造随机ID；工具调用ID仅留SDK协议和原始审计，普通工具详情不显示。内容hash用于程序完整性校验，不作模型来回抄写的业务ID。

恰好一份原文时source_id可省略；多原文不猜测。`locate_source({quotes:["引文甲","引文乙"]})` 批量定位，结果按输入index关联坐标，不重复回显原句与hash。`count_is_exact=false` 时match_count仅为已知下界，truncated明确标识未返回全部。

常规提取直接`submit_extraction`，程序自动定位与静态检查；合法立即原子入库，问题批持久暂存，只按反馈的submission_id和问题索引局部patch。成功仅返回必要状态、修订和计数；暂存不是人工审批候选，也不算正式入库。完整诊断、原始模型消息及存疑理由保留宿主审计，preview仅用于独立试查。

层级为主要故事（storyline/T）→大事件（big_event/B）→场景（scene/S）→事件（event/E）→动作（action/A）。主要故事仅在模型判断完整篇章真正结束后总结：可用 `children:[B1,B2]` 归并已有大事件，无需重列或手填引文。程序按正式来源顺序及 Unicode 坐标，从下级已验证证据的最后末端派生 `completion`，不要求 `closing_quote`，不以本批或章末自动闭合。未结束的大事件可暂不归主要故事。主要故事进展 `storyline_progress` 记录在大事件上，大事件进展 `big_event_progress` 记录在场景上。

大事件在同语境内依原文顺序形成 `[start,next.start)` 的连续阅读区间。末个大事件已归入完成的主要故事时，以该主要故事的 `completion` 为末端；否则保持 `end=null`。这不是故事发生时间区间。`attach_children(parent_id,child_ids)` 可直接归入已有直属子项，主要故事归大事件、大事件归场景、场景归事件（或嵌套场景）、事件归动作；不保留旧场景直属主要故事的接口。

关系方向统一以主体为参照：`node=A,property=父亲,value=B` 表示“B是A的父亲”，`property` 描述对方相对 `node` 的身份、关系或作用中的角色，节点型 `value` 为被指向的对方ID。普通 `string/any` 内容仍按字面保存。程序不依据关系词表翻转方向，不因对称关系补造反向记录。

`record_property_change` 对新属性定义必须显式提供顶层 `value_type`；已有定义可省略沿用。节点引用使用 `node/either`，`string/any` 中的P样字符串仍是字面内容，不画作人物关系。可显式提供严格布尔 `symmetric`（新定义默认false）；与已有类型或对称标识冲突时原子拒绝，须另行显式修改定义，不能暗中改写。

关系核对不仅看长期身份与称谓，也包括对叙事有意义、原文明示已发生的人际作用角色；不因一次发生就排除，也不由共现或单次作用外推长期立场。任务完成情况以实际工具返回为准，不将未调用或未成功的工具声称为已完成。

人物心理意图、所知所信也需查漏。`scope=world` 保存原文支持的客观事实；`cognition` 保存人物所知所信或主观判断，`claim` 保存言说主张，后二者须用 `holder` 标明认知者／说话人，`node` 仍为被认知／谈论的主体。经旁证支持的对白内容可属于世界事实，不按对白形式机械全归主张；不同scope及holder不能覆盖客观取值。


## 事件属性变更（当前唯一合同）

草稿 `properties` 每项必填 `node/property/at_event/op/quotes`；`op` 为 `set/add/remove/clear/unknown`。前三者须提供 `value`，后两者禁止携带 `value`。不再接收 `expirations`、`valid_from`、`valid_to` 或模型填写的 `sequence`。程序用自动定位的精确引文起点生成 sequence，并按事件顺序与 sequence 推导状态；one人物关系按对方分别维护，many表示类型化集合数组。

手动补录使用 `record_property_change(node_id,property_id,event_id,op,expected_revision,reason,...)`，程序分配同事件后继 sequence。新剧情变化追加该记录；错误录入用 `correct_property(record_id,changes,expected_revision,reason)` 更正（sequence保留）或 `delete_property_records(record_ids,expected_revision,reason)` 物理删除。`clear` 表示明确清空，`unknown` 表示明确未知；缺失不自动等于两者。

CLI 对应 `record-property-change`、`correct-property`、`delete-property-records`，完整类型和必填参数以 `--help` / 当前工具schema为准。CLI、MCP、Qt共用Operations原子引用、证据及并发校验；不提供旧有效期接口或兼容映射。


## 剧本来源标注

剧本场表可选填写 `plan.source_event_ids`，不作为生成前置，不自动把空引用填为当前全部候选。已填写的编号仍须是本剧本有效范围内的真实事件；名称、时间线和来源映射由程序派生，空记录不显示“尚未标注”注记，也不新建说明正文块。生成成功结束后，时间线对明确未使用的活动事件低亮显示，事件仍可选择和编辑。`event_usage` 区分 `used_event_ids`、`unused_event_ids`、`unknown_event_ids`；只有启用且有正文场的显式实际来源计为已使用，未绑定部分和自动继承的候选不能冒充实际使用，未确认节点不低亮。完成状态只读本工程真实任务审计，运行、停止、失败和未生成时不启用低亮；重启后仍按持久任务结果恢复，不依赖界面内存。此提示不是语义一致性校验，也不要求模型补写使用或未使用清单。

## 统一业务内容暂存

业务内容操作在 Operations 层共用暂存与局部修补；Qt 直接调用、CLI、MCP 和助手行为相同。`staged` 只表示草稿保留，不能记作正式写入。反馈包含 `submission_id`、`submission_revision` 和具体问题路径；已知修正内容时直接一次 patch 修多点，不先读取整稿。

- `list_content_submissions(operation=None,target=None,limit=20,after=0)`：仅在恢复时分页找回待修摘要。
- `read_content_submission(submission_id,paths=None)`：默认只读状态和问题；显式 paths 仅读取指定局部内容。
- `patch_content_submission(submission_id,expected_revision,patches,reason)`：修订号为暂存修订；每项为 `{op:set|remove|append,path:[字段名或0基索引,...],value?}`。remove不带value，set/append带value。

路径从原 Operations 已绑定参数开始；提取保持原数组索引和 `@n` 引用。不得删除数组行或任意重排，不得替换操作、目标、权限、业务修订号或上下文依据。修补始终按当前权限与依赖重检，通过即自动入库；过期依据须重新读取后明确新提交，不能由程序自动刷新修订掩盖冲突。

CLI 别名为 `content-submissions`、`content-submission`、`content-patch`，也可使用通用 `tool call`。提交错误时 UI 保留输入并显示待修位置，同表单再次保存修补同一暂存；仅成功才关闭或继续后续动作。不增加审批工作台或人工 accept。

删除、移动、复制、采用/批准、文件导入导出、生成/外部服务、密钥配置等即时命令继续原有原子执行，不进入待执行队列。混有即时命令的批次不被通用修补延后执行。非法工具 JSON、权限、并发、IO/网络及未知异常仍按原规则反馈，不假装内容校验已成功。


## 分镜资产元素

每镜复用 `visual_elements` 保存出场元素。人物的 `name` 为角色名，人工确定的 `asset_id` 指向资产管理中的具体形象；物品与场景分别关联对应类别的资产卡。资产卡可以独立创建，`node_id` 可空；明确关联提取或改编事实时仍验证所选数据域与节点类别。

多选或全选镜头后，以固定 `shot_ids` 发起 `storyboard_assets` 统一任务。`read_storyboard_assets_context` 返回所选镜头、正文、已确定元素及资产目录，模型只分析尚待确定的元素需求，提交 `kind/name/usage`；未确定行的空资产引用由程序生成。分析任务不能建立或修改资产，也不能自动选择资产。原已确定行原样保留，新分析替换原未确定行，不按同名猜测或改写已有身份。

`submit_storyboard_assets` 原子保存指定镜头的分析结果。内容问题保留暂存并反馈元素字段路径，局部修补通过后自动入库。原目标镜头、修订和正文依据不可通过修补替换。UI、CLI、MCP 和助手共用 Operations 合同。

用户可在元素表明确选择已有资产，或点击“建立缺失资产”。该按钮调用 `create_missing_storyboard_assets` 即时原子命令，程序为所选待确定元素逐行建立独立空资产卡并绑定；不调用模型或生成图片，不按同名猜测合并。省略 `element_refs` 处理所选镜头全部未确定行，指定 `{shot_id,index}` 则仅处理明确行；整板 `revision` 固定行定位，重复或过期操作不会重复创建。已绑定行保持原样，损坏引用仍需手动处理。空卡不填写设定、提示词或图片，后续可编辑。

列表也保留“详细建立”，通过原 `save_asset` 和分镜写入两步执行；若资产已建成而绑定并发失败，明确反馈关联未完成。普通助手对话仍可按用户指令建立或选择资产；批量分析不会自动调用这些写入能力。人物显示为角色名与具体形象名。

元素表区分待确定、资产缺失、类别不符与待出图。未分析的空表显示待分析，无图片仅提示待出图，不等同于缺少资产对象。语义漏项由模型判断，静态检查不靠词表猜测。
