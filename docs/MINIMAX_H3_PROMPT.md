# MiniMax H3 提示词官方规范

用途：给 `src/narrative/segment_motion.py` 的提示词拼装（`运镜/主体/环境/约束` 四段）与人工改写提供官方口径：官方结构、运镜词表、两种生成意图的差异。结论取官方页面，社区经验单独标注，查不到的写「未见官方说明」。

## 1. 官方来源

| 来源 | 内容 | 地址 |
| --- | --- | --- |
| 官方仓 README | 模型规格（4–15 秒、768P/2K、24FPS、32kHz 立体声）、九个 skill 清单、Context-IR/Base/Regenerate-2K 三段式 | [github.com/MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3) |
| `skills/h3-prompt-writing/SKILL.md` | 提示词 skill 入口：模式判定、字段顺序、输出规则、四条 tips | [SKILL.md](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/SKILL.md) |
| `references/base-en.txt` | 官方向导《Video Prompt Writing Guide (T2VA / I2VA / FL2VA / L2VA)》：三段字段、首帧/首尾帧指令句、切镜与运镜词表 | [base-en.txt](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/base-en.txt) |
| `references/ref-en.txt` | 官方向导《Full-Reference Mode Rewrite Output Format Guide》：参考生视频六段与保留度标记 | [ref-en.txt](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt) |
| 视频生成指南 | 三种生成模式、输入上限、提示词 ≤7000 字符 | [video-generation.md](https://platform.minimax.io/docs/guides/video-generation.md) |
| 创建任务 API | `content[]` 的 `role`、提示词必填、`ratio` 规则、参考与首尾帧互斥 | [video-generation-v2-create.md](https://platform.minimax.io/docs/api-reference/video-generation-v2-create.md) |
| H3-Context-IR API | 官方前置改写服务：只返回增强后的提示词，不生成视频 | [h3-context-ir.md](https://platform.minimax.io/docs/api-reference/video-generation-v2-h3-context-ir.md) |
| H3 能力示例页 | 官方成品提示词画廊（多图参考、时长/画幅前缀写法） | [guides/video-prompt.md](https://platform.minimax.io/docs/guides/video-prompt.md) |

未能证实、故不写入结论：中文提示词指南页（`platform.minimaxi.com` 本次未取到可读页；未见独立中文规范）；HuggingFace 上的同名指南（README 说明指南随 HF 发布，本机无法直连，`references/*.txt` 即可读副本）；负面提示词（`text` 只有正向字符串，API 无 `negative_prompt`，两份向导也无负面词段落，只能算社区经验）。

## 2. 官方推荐结构

官方不给「主体+动作+场景+镜头+风格」单行公式，而是按模式给**固定字段与固定顺序**；字段名原样保留，正文写成自然英文段落（对白、歌词、画面文字保留原文）。

### 2.1 基础模式（T2VA / I2VA / FL2VA / L2VA）

先首行指令（T2VA 无），空一行，再三段：`integrated_multimodal_description` → `overall_soundscape` → `non_diegetic_music`。

| 段 | 写什么 | 篇幅（官方） |
| --- | --- | --- |
| `integrated_multimodal_description` | 按时间轴写画面、动作、镜头、说话人、对白、画内声 | 无字数硬指标，须覆盖全时长 |
| `overall_soundscape` | 环境声、物理动作声、非语言人声；不重复对白与画内音乐 | 1–4 句，一段 |
| `non_diegetic_music` | 只有观众听得到的配乐：器乐、速度、节奏、强弱变化 | 1–3 句；无配乐写 `N/A` |

首行指令是三句官方模板，必须是提示词第一行（`N` 为最后一镜编号，`S.SS` 为时长两位小数）：

| 模式 | 官方首行（逐字照抄，只换 N 与 S.SS） |
| --- | --- |
| I2VA | `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.` |
| FL2VA | `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.` |
| L2VA | `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.` |

### 2.2 全参考模式（Ref2VA）

六段，顺序固定：`subject_definitions` → `summary` → `retention_analysis` → `detailed_description` → `overall_soundscape` → `non_diegetic_music`。

| 段 | 作用 |
| --- | --- |
| `subject_definitions` | 定义 `<Subject N>`/`<Picture N>`/`<Video N>`/`<Audio N>` 指什么、承担什么角色 |
| `summary` | 一段话概括任务类型前缀（如 `[reference generation]`）与参考关系 |
| `retention_analysis` | 逐标签写保留程度（见下表） |
| `detailed_description` | 主体正文；生成类常态 350–500 词；风格在 `[Shot 1]` 之前一两句交代 |

| 标记 | 用于 | 含义 |
| --- | --- | --- |
| `fully_preserved` / `partially_preserved` | 画面类 | 定义的角色完全保留／只部分保留 |
| `attribute_transfer` | 画面类 | 特征转移到另一个可识别主体 |
| `weak_reference` | 画面类与音频类 | 只保留风格、类别、构图或氛围的宽泛相似 |
| `fully_copy` / `partially_copy` / `reference` | 音频类 | 整段复制／部分复制／只参考音色节奏 |

标签编号在两类间独立（同一素材可既是 `<Video 1>` 又是 `<Audio 2>`）；`<Video N>` 只表达整段视频关系（编辑源、接续、运镜与节奏结构），具体人物或场景仍归 `<Subject N>`。参考视频带声音本身不自动产生 `<Audio N>`。

### 2.3 API 侧硬约束

| 项 | 规则 |
| --- | --- |
| 提示词 | 每个请求必须有一条非空 `text`；≤7000 字符 |
| 时长 | 4–15 秒整数；正文描述的总时长要与请求时长一致 |
| `ratio` | t2va 必填且不能为 `adaptive`；i2va（首/尾帧）恒为 `adaptive`，传其他值被忽略；r2va 可选默认 `adaptive`（`MiniMax-H3-Lite` 不支持） |
| 输入 | 首尾帧最多 1+1 张；参考图 ≤9、参考视频 ≤3 段（每段 2–15 秒、总 ≤15 秒）、参考音频 ≤3、混合 ≤12 文件；首尾帧与参考类**不可混用** |
| 镜头记法 | `[Shot 1]` 不加时间戳；后续镜 `[Shot 2] At 00:03.500, ...`，时间严格递增且在时长内 |

## 3. 官方运镜词表

来源：`base-en.txt` §4.3「Camera Motion: Motion Type + Amplitude + Speed」。官方拆三个维度：**类型**（怎么动）、**幅度**（构图变化范围）、**速度**（变化快慢）；幅度与速度只在有意义时写，中等幅度与常速默认省略。共 **12 类 20 条表达 + 4 条修饰**。

| 官方表达 | 中文 | 说明（官方） |
| --- | --- | --- |
| `Zoom In` / `Zoom Out` | 变焦推近／拉远 | 机身不动，只改焦距 |
| `Push In` / `Pull Out` | 机身推进／后拉 | 相机前后移动 |
| `Pan Left` / `Pan Right` | 左摇／右摇 | 机身不动，镜头水平转 |
| `Truck Left` / `Truck Right` | 左移／右移 | 机身水平平移 |
| `Tilt Up` / `Tilt Down` | 上摇／下摇 | 机身不动，镜头垂直转 |
| `Pedestal Up` / `Pedestal Down` | 升／降 | 整机上下移动 |
| `Arc Shot` | 环绕 | 绕主体弧线运动 |
| `Tracking Shot` | 跟拍 | 跟随运动主体 |
| `Static Shot` | 静止 | 机位与镜头都不动 |
| `Shake Slightly` / `Shake Strongly` | 轻微／强烈抖动 | 手持或震动感 |
| `POV` | 主观视角 | 以主体视角拍摄 |
| `Roll Clockwise` / `Roll Counterclockwise` | 顺时针／逆时针滚转 | 绕镜头轴旋转 |
| `with small amplitude` / `with large amplitude` | 小幅／大幅 | 幅度修饰 |
| `at slow speed` / `at fast speed` | 慢速／快速 | 速度修饰 |

官方的三条写法示例：

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
The camera pans right with large amplitude at fast speed, revealing the open doorway.
The camera holds a static shot as the runner exits the frame.
```

- 运镜写在镜内的自然英语动作里，**不要**在句尾堆独立标签。
- 切镜要带来新信息（主体、空间、状态、视角、时间）；只需改景别或轻微角度时，官方要求优先用运镜。
- 旧版 Hailuo-02 的方括号命令（`[Pan left]`、`[Push in]`、`[Static shot]` 等 15 条，见[旧版首尾帧接口](https://platform.minimax.io/docs/api-reference/video-generation-fl2v.md)）是旧模型口径；H3 V2 的 `text` 无命令解析，官方 H3 向导也不用方括号。

## 4. 参考生视频（我们的 `reference_motion`）

官方要点（`ref-en.txt` + SKILL）：标签先定义后使用、跨段不改含义、不给未定义标签；`detailed_description` 每镜要交代构图、主体外观与位置、环境与光位、动作与状态变化、运镜、当前声音，以及参考素材真正生效的位置，不要退化成剧情梗概或参考关系清单；风格句放在 `[Shot 1]` **之前**；只借运镜、节奏、时间结构时用 `<Video N>` + `weak_reference`，并在正文点明生效镜头；tips 要求描述总时长等于请求时长、标签全局一致、少用 `cinematic`/`beautiful` 这类抽象词、关键帧要说清怎么接进时间轴。

示例 A（普通描述 → Ref2VA 六段骨架；正文英文）

```text
普通写法：一个女孩在雨夜车站等车，镜头慢慢靠近她。
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, keeping her face, wet short hair, beige trench coat and canvas tote.
<Video 1> is the clean sketch animatic; it provides camera path, subject blocking and beat timing only.
summary:
[reference generation] The target video keeps <Subject 1> from <Picture 1> and follows the camera path and beat timing of <Video 1> on a rain-soaked platform.
retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - identity, costume and tote unchanged.
<Video 1> (camera path and rhythm): weak_reference - only the push-in direction and the two-second hesitation beat are followed.
<Picture 1> (style anchor): fully_preserved - palette, light direction and grain kept.
detailed_description:
The target video is live-action and cinematic, with a cool, rain-soaked night palette.
[Shot 1] The young woman from <Subject 1> waits under the canopy ... The camera pushes in with small amplitude at slow speed toward her hands as she tightens her grip on the tote. Her shoulders drop as the beat from <Video 1> lands.
overall_soundscape: Steady rain drums on the canopy while a train idles two platforms away ...
non_diegetic_music: N/A
```

示例 B（只想借运镜时的最短写法）

```text
普通写法：参考这段动画的镜头运动做真人版，主角换成我们的女主。
subject_definitions:
<Video 1> is the camera-and-rhythm reference for the target video, nothing else.
<Subject 1> is the woman in <Picture 1> (identity, wardrobe, hairstyle).
summary:
[reference generation] The target video reproduces <Video 1>'s lateral tracking and three-beat reveal while replacing performer, costume and set.
retention_analysis:
<Video 1> (camera and rhythm): weak_reference - truck direction, two blocking changes and beat spacing reused; performer, costume, set and audio not.
<Subject 1> (appears in [Shot 1]): fully_preserved.
detailed_description:
Live-action, cinematic. [Shot 1] <Subject 1> walks the corridor ... The camera trucks right with small amplitude at slow speed until the doorway enters frame on the same beat as <Video 1>.
```

## 5. 首尾帧生视频（我们的 `endpoints`）

官方要点（`base-en.txt` §3.2 + Case 3）：首行照抄 2.1 的 FL2VA 对齐句后空一行；Picture 1 是开头、Picture 2 是结尾，正文**写两者之间的路径**（主体怎么动、姿态怎么变、物件怎么被操作、构图怎么演变、场景与光位怎么过渡），不要复述两张静态图；FL2VA 一般用**单镜头**便于从首帧连续插值到尾帧，多镜只在确实被要求时用；尾帧必须由最后一镜真正到达；推荐结构是**首帧状态 → 可观察的中间变化 → 差异逐步收窄 → 尾帧状态**。

示例 C（把因果链写出来，不重复两端）

```text
普通写法：男人从门口走到窗边，最后停在窗边看外面。
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the 8.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, the man begins in the framing, posture and lighting established by Picture 1 at the doorway, one hand still on the frame. He releases the handle and crosses the room in three unhurried steps as the camera trucks right with small amplitude at slow speed ... he stops at the window, lifts his chin, and settles into the spacing, shoulder angle and window-light falloff established by Picture 2 at the end of the shot.
overall_soundscape: A latch clicks free, floorboards creak under three steps, distant traffic hums below.
non_diegetic_music: N/A
```

示例 D（双图过渡，别写成两句静帧）

```text
普通写法：用这两张图做过渡，前面是一杯咖啡，后面是空杯子。
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the 6.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a close shot opens on the full cup in Picture 1's position and light. Steam thins as the cup is lifted, tilted and set back down empty; the camera holds static until the final second, then pushes in with small amplitude at slow speed as the last ring of residue settles into the cup position, rim highlight and tabletop shadow of Picture 2 at the end of the shot.
```

对照：普通写法只说两端；官方要求两端各用一句锚定，中间写可观察的变化。

## 6. 常见失败与规避

| 现象 | 依据 | 规避 |
| --- | --- | --- |
| 第一镜加时间戳；后续镜时间不递增或超出时长 | 官方（§4.2） | 首镜只写 `[Shot 1]`；后续镜 `At MM:SS.mmm` 严格递增 |
| 运镜写成句尾标签堆叠（`push in, slow, 5%`） | 官方（§4.3） | 句内动作：类型 +（幅度）+（速度） |
| 首尾帧正文复述两张静帧 | 官方（§3.2） | 只写路径与逐步收窄的差异，两端各一句锚定 |
| 描述总时长与请求时长不一致 | 官方 tips | 对齐句 `S.SS` 与镜内切点都按实际时长 |
| 用了未定义或跨段改义的 `<Subject N>` | 官方（ref §2） | 先定义后使用，全文一致 |
| 用剧情梗概代替画面 | 官方（ref） | 每镜写构图、位置、光位、动作与状态变化 |
| 只为改变景别就切镜 | 官方（§4.2） | 优先运镜；切镜必须带来新信息 |
| 堆 `cinematic`、`beautiful` 等抽象词 | 官方 tips | 换成具体可视细节；风格标签只在开头一句声明 |
| 对白被翻译或改写；画面文字没加英文双引号 | 官方（§4.4、§4.5） | 对白放 `<d>[语言] 原文</d>`；画面文字原样加双引号 |
| 声音段重复写对白或画内音乐 | 官方（§4.6） | 对白与画内声留在正文，声音段只写环境与物理声 |
| 负面清单（"不要多余手指""避免文字"）、多人多手、画面文字乱码、强运动或过快切镜失败率高 | 社区经验，未见官方说明 | 不写负面清单，改成正向描述正确状态；按官方结构收敛为单镜头、单目标动作、具体细节，其余靠试跑验证 |
| 用中文正文 | 未见官方说明（官方要求英文写各段） | 正文英文；对白、歌词、画面文字保留原文 |
| 把剧本整段塞进提示词 | 官方（≤7000 字符、逐镜可观察细节） | 只放本镜可观察内容；长剧本走 H3-Context-IR 或人工改写 |

## 7. 映射到我们的四段结构

`segment_motion.py` 的 `segment_prompt` 输出四行中文（`运镜：`／`主体：`／`环境：`／`约束：`），bbox 与焦距留在结构化字段、不进提示词。官方没有这四个字段名，故四段只作内部结构，发请求前折成官方字段。

| 我们的段 | 官方对应 | 谁产出 | 依据 |
| --- | --- | --- | --- |
| 运镜 | §4.3 类型 + 幅度 + 速度 | 程序：`CAMERA_MOVES` 16 种（推拉/横移/升降/变焦/摇/俯仰/跟拍/固定/切镜）、`CAMERA_SPEEDS` 3 档；**优先由预演的机位轨迹、焦距关键帧与朝向变化量出**（`source=previs`），没有预演时由框图差量估计（`source=layout`），人工标注可覆盖（`source=manual`） | 运镜须写成镜内自然英语动作，不并列标签 |
| 主体 | 正文主体与动作变化；`<Subject N>` 首现定义 | 人写分镜稿；程序补左右／居中、靠近／远离与「第 N 个主体」序号 | 写随时间的变化，标签先定义后使用 |
| 环境 | 风格开头句 + 场景、光位、道具 | 人写（分镜稿与创作约定）；程序只写「由 X 转为 Y」 | 风格句在 `[Shot 1]` 前（参考）或开头（基础） |
| 约束 | 官方无负面清单，只有硬约束与保持项 | 程序（格式、互斥、时长）+ 人写（身份、服装、场所保持） | §2.3 硬约束、§4.2 切镜、§4.4 对白 |

规则：

1. **运镜换官方词**：推近→`Push In`、拉远→`Pull Out`、左摇／右摇→`Pan Left`／`Pan Right`、上摇／下摇→`Tilt Up`／`Tilt Down`、跟移→`Tracking Shot`（横向匀速可用 `Truck Left/Right`）、固定→`Static Shot`；速度 `slow`／`fast`→`at slow speed`／`at fast speed`，`medium` 省略；幅度按景别差或画面占比变化取 `with small/large amplitude`。句内写成 `The camera <type> with <amplitude> at <speed> ...`。
2. **数字不进正文**：bbox、像素位移、焦距、裸毫秒留在结构化字段；位移换成构图变化，毫秒只出现在镜内切点 `MM:SS.mmm` 与对齐句 `S.SS`（现「时长约 N 毫秒」应改成这条记法）。
3. **`约束` 段去否定化**：`不得／不许` 改成正向保持项（身份、数量、服装、场所不变；单镜头、只表现本段过渡），格式硬约束留在此段；「视频模型不接受运镜参数」与 §4.3 不符，运镜用正文自然英语表达即可。
4. **`endpoints` 先放对齐句再写正文，只写过渡**：首行逐字用 FL2VA 模板（`S.SS` = 请求整秒时长两位小数），空一行，再写单镜头路径，末尾落到 `Picture 2`；不复述首帧，此模式不加参考标签。
5. **`reference_motion` 用 Ref2VA 六段**：草图动画定义成 `<Video 1>`（只承担运镜、调度、节拍 → `weak_reference`），风格帧定义成 `<Subject N>`／`<Picture N>`（`fully_preserved`）；正文点明参考生效位置，不复制草图线稿与背景，「第 N 个主体」首现处要补一句可识别外观。
6. **正文英文、字段名与顺序与标签原样保留**；对白、歌词、画面文字保留原文。四段整体放进 `integrated_multimodal_description`（或 `detailed_description`）内，不自造顶层字段。
7. **声音两段必须出现**：缺省 `N/A`（只有全片静音才允许环境声 `N/A`），不重复对白。
8. **≤7000 字符**：分镜稿、创作约定、剧本上下文摘取不搬运；超长先走 H3-Context-IR 或人工改写。

优先落地三条：规则 1（运镜换官方词表＋句内动作）、规则 3（约束段去否定化）、规则 4（首尾帧对齐句＋只写过渡）。

## 8. 来源

- 官方仓：[MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)（README、[SKILL.md](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/SKILL.md)、[base-en.txt](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/base-en.txt)、[ref-en.txt](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt)）
- 官方平台文档：[Video Generation](https://platform.minimax.io/docs/guides/video-generation.md)、[创建任务 V2](https://platform.minimax.io/docs/api-reference/video-generation-v2-create.md)、[H3-Context-IR](https://platform.minimax.io/docs/api-reference/video-generation-v2-h3-context-ir.md)、[H3 Feature Highlights](https://platform.minimax.io/docs/guides/video-prompt.md)
- 仅用于对照旧口径：[旧版首尾帧接口（Hailuo-02 方括号命令）](https://platform.minimax.io/docs/api-reference/video-generation-fl2v.md)
