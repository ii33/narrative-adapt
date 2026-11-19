# 图像与视频模型调研：FLUX 3 Image Klein / Qwen-Image-2.1 / Krea 2 / MOSS-Image / MiniMax H3

> 用途：为「叙事改编 → 分镜 → 素描图稿 → 风格化关键帧 → 视频」这条管线做图像与视频模型的选型依据，并作为后续编写 ComfyUI 工作流的输入。能力项一律标来源（`S1`…，见文末清单），官方页面没写的写「未见官方说明」，不按第三方媒体补空。

## 一、速览

| 模型（官方名） | 发布方 / 时间 | 生成规模 | 开源与权重 | 许可（要点） | ComfyUI 门槛 |
| --- | --- | --- | --- | --- | --- |
| **FLUX 3 Image Klein**（Distilled / Base） | Black Forest Labs，2026-10-15 开放权重 `S2` `S1` | 5.6B `S1` | 开源权重，HF `black-forest-labs/flux-3-image-klein`（+`-base`、`-pup`、`-comfy`）`S2` `S4` | FLUX Kommunity License（权重）/ Apache 2.0（代码、upsampler）`S2` | 0.41.0+ `S4` |
| **Qwen-Image-2.1**（+Turbo） | 阿里通义，2026-09-20（Turbo 2026-10-09）`S5` | 视觉生成 7B，32 层单流 DiT `S5` | 开源权重，HF / ModelScope `Qwen/Qwen-Image-2.1`，ComfyUI 版 `Comfy-Org/Qwen-Image-2.1` `S5` `S6` | Qwen Research License：**仅非商用** `S7` | 原生支持；官方教程只要求「更新到最新」`S6` |
| **Krea 2**（RAW / Turbo，官方亦称 K2） | Krea AI，2026-06 开放权重 `S8` `S9` | 官方仓库未标参数量（第三方记 12B，未获官方确认） | 开源权重，HF `krea/Krea-2-Raw`、`Krea-2-Turbo`，ComfyUI 版 `Comfy-Org/Krea-2` `S8` `S9` | Krea 2 Community License v.1（年营收 <100 万美元可商用）`S9` | 官方教程未标版本号 `S9` |
| **MOSS-Image**（Base / SFT） | OpenMOSS / MOSI，2026-10-16 `S11` | 5B 单流 DiT `S11` | 开源权重，HF `OpenMOSS-Team/MOSS-Image-Base`、`-SFT`，ComfyUI 版 `Comfy-Org/MOSS-Image` `S11` `S12` | 代码 Apache 2.0；**权重许可待确认**（见 §八）`S11` `S12` | 文生图 0.42.0+；编辑/色深控制 0.43.0+ `S12` |
| **MiniMax H3**（H3 / H3-Max 等） | MiniMax，API 服务（非本地权重）`S14` `S15` | 未见官方公开参数量 | 闭源 API；`POST /v2/video_generation` `S15` | 未见官方说明（见 §五）`S16` | 不适用（云端） |

## 二、FLUX 3 Image Klein

| 项目 | 结论 |
| --- | --- |
| 发布方与时间 | Black Forest Labs；2026-10-15 开放权重发布（权重仓库首发提交日）`S2`，官方文档页 2026-11-03 更新 `S1` |
| 参数规模 / 架构 | 5.6B，两个 checkpoint 同规模；官方页只给参数、步数、分辨率，未描述骨干网络细节 `S1` `S2`。ComfyUI 侧配套：Qwen3-VL-4B 文本编码器、FLUX.2 VAE `S4` |
| 开源与否 / 权重位置 | 开源权重：`black-forest-labs/flux-3-image-klein`（52 GB 快速版）、`-base`（107 GB 全量）、`-pup`（提示词上采样器）、`-comfy`（单文件 bf16 / int8 / nvfp4）`S2` `S4` |
| 参考图上限 | 最多 10 张；prompt 里用尖括号包裹的 `ref_image_0` … `ref_image_9` 标签名指代；编辑时第 1 张决定输出尺寸 `S2` `S4` |
| 布局 / 框控制 | **有原生边界框**：框写进 `prompt` 的 JSON 行，坐标 `[top, left, bottom, right]`，整数 0–1000；框只引导构图与尺度，不是裁剪遮罩 `S3`。ComfyUI 模板内置 `Create Bounding Boxes` 节点 `S4` |
| 局部编辑（mask / 圈选 / 涂抹） | 官方走「指令 + 参考图 + 框行」路线：每行用 `from` / `src_bbox` / `tgt_bbox` 表达保留、移动、新增、删除；删除后由模型补背景 `S3`。**未见官方说明**支持独立 mask / 涂抹输入 |
| 风格参考 | 未见独立「风格参考」开关；风格靠文字描述、参考图与 Base 上的 LoRA（官方给了 LoRA 训练与在 Distilled 上加载的流程）`S1` `S2` |
| 分辨率与步数 | 最高 4K；Distilled 16 步、无 CFG，Base 50 步、CFG 4；ComfyUI 文生图模板给 Base 50 步 / 3.5 `S1` `S2` `S4` |
| 显存门槛 | 官方实测（B200，1024²）：bf16 31 GB、fp8 26 GB、nvfp4 25 GB，upsampler 另占 `--pup-memory`（默认 0.35）；官方称可落到 RTX 3090；ComfyUI 的 int8 文件可跑 8 GB（RTX 3070 级）`S2` `S4` |
| ComfyUI 支持 | 需 ComfyUI 0.41.0 或更新（0.41.1 回移，ComfyUI#17070）；教程 <https://docs.comfy.org/tutorials/flux/flux-3-klein> `S4` |
| 已知限制 | 官方强要求配 prompt upsampler，直连原始提示词质量明显更差；元素可略微超出框；新元素框约 40×25 px 时常常不出现；编辑时框表与指令要说法一致 `S2` `S3` |

## 三、Qwen-Image-2.1

| 项目 | 结论 |
| --- | --- |
| 发布方与时间 | 阿里通义 Qwen 团队；2026-09-20 发布 2.1，2026-10-09 发布 Turbo（8 步）`S5` |
| 参数规模 / 架构 | 视觉生成组件 7B（32 层单流 DiT，块因果注意力）；文本与条件图编码用 Qwen3-VL 8B；VAE 为 64 通道 RGBA、16× 空间压缩 `S5` |
| 开源与否 / 权重位置 | 开源权重：HF / ModelScope `Qwen/Qwen-Image-2.1`、`-Turbo`；ComfyUI 单文件在 `Comfy-Org/Qwen-Image-2.1` `S5` `S6` |
| 参考图上限 | 官方写最多 10 张；ComfyUI 编码节点最多 16 个槽（`image_1`…`image_16`），两个编辑模板接前 10 张，`image_1` 为被编辑图 `S5` `S6` |
| 布局 / 框控制 | **未见官方说明**存在 bbox 输入（官方能力列表里只有参考图、圈选/涂抹/mask、透明通道）`S5` `S6` |
| 局部编辑（mask / 圈选 / 涂抹） | 支持：官方明确「圆圈、涂抹标注或独立 mask」做局部编辑，并强调人像与商品的身份保持 `S5`。ComfyUI 做法是在 `LoadImage` 上用 Paint Pen 把改动区涂在 RGB 上（不进遮罩），提示词里点名标记颜色 `S6` |
| 风格参考 | 未见专门风格参考通道；靠参考图 + 提示词（官方 showcase 有「三视图角色参考生成分镜」）`S5` |
| 分辨率与步数 | 原生 2K（推荐尺寸表到 2752×1536 等）；默认 40 步；Turbo 8 步、CFG 1；ComfyUI 基础模板 25 步 / cfg 1、Turbo 模板固定 8 步 `S5` `S6` |
| 显存门槛 | **未见官方说明**具体 VRAM 门槛，官方只给 `enable_model_cpu_offload` 与 int8 / bf16 两种打包 `S5` `S6` |
| ComfyUI 支持 | Day-0 原生支持；教程 <https://docs.comfy.org/tutorials/image/qwen/qwen-image-2-1>；页面未标最低版本号，只要求更新到最新（含每夜版）`S5` `S6` |
| 已知限制 | 许可仅非商用；自带 VAE 在写实细节上有棋盘状纹理（官方文档建议换非官方 texture-fix VAE）；远超 2K 的目标会掉提示词遵循度；cfg 1 时负向提示词不生效；提示词增强器开 thinking 需把 `max_length` 提到模型卡值，否则返回空串 `S6` `S7` |

## 四、Krea 2

| 项目 | 结论 |
| --- | --- |
| 发布方与时间 | Krea AI；2026-06 开放权重，社区许可 v.1 日期 2026-06-22 `S8` `S9` |
| 参数规模 / 架构 | 官方仓库与 ComfyUI 教程均未给参数量与骨干描述（第三方记 12B DiT，未获官方确认）；文本编码器 Qwen3VL-4B、VAE 复用 Qwen-Image VAE `S8` `S9` |
| 开源与否 / 权重位置 | 开源权重：HF `krea/Krea-2-Raw`（未蒸馏基座）、`krea/Krea-2-Turbo`（8 步蒸馏）；ComfyUI 版（fp8 / int8 / nvfp4）与官方风格 LoRA 在 `Comfy-Org/Krea-2` `S8` `S9` |
| 参考图上限 | 本地：官方风格参考工作流用 1–2 张（配套专用扩散模型 + 风格参考 LoRA）；托管节点：最多 10 张风格参考 + moodboard `S9` `S10` |
| 布局 / 框控制 | **未见官方说明**（无 bbox 输入）`S9` `S10` |
| 局部编辑（mask / 圈选 / 涂抹） | **未见官方说明**（只发布文生图 checkpoint，没有编辑模型）`S8` `S9` |
| 风格参考 | 核心能力：风格参考图 + 一组官方风格 LoRA（9 个，各有触发词与推荐强度 1.0）+ moodboard `S9` `S10` |
| 分辨率与步数 | RAW：52 步、CFG 3.5，官方称训练到 1K；Turbo：8 步、CFG 0、`mu 1.15`，可出 1K–2K `S8` `S9` |
| 显存门槛 | **未见官方说明**具体门槛；ComfyUI 教程只给 fp8 / int8 / nvfp4 几种打包 `S9` |
| ComfyUI 支持 | 教程 <https://docs.comfy.org/tutorials/image/krea/krea-2>（含风格参考与风格 LoRA 工作流）；未标最低版本号 `S9` |
| 已知限制 | 商用受营收门槛与内容过滤义务约束（见 §五）；RAW 只在约 1K 分辨率训练；无编辑能力，局部修改只能回到出图重跑或换模型 `S8` `S9` |

## 五、MOSS-Image

| 项目 | 结论 |
| --- | --- |
| 发布方与时间 | OpenMOSS / MOSI.AI；2026-10-16 发布 Base 与 SFT 权重与代码 `S11` |
| 参数规模 / 架构 | 5B 单流 DiT；ComfyUI 侧配 Qwen3.5-9B 文本编码器（`CLIPLoader` 类型 `moss_image`）与 FLUX.2 small VAE `S11` `S12` |
| 开源与否 / 权重位置 | 开源权重：HF `OpenMOSS-Team/MOSS-Image-Base`、`-SFT`；ComfyUI 单文件（int8 / bf16）在 `Comfy-Org/MOSS-Image`；几何估计另用 `Comfy-Org/MoGe` 的 MoGe 3 `S11` `S12` |
| 参考图上限 | **模型没有图像输入**（官方 ComfyUI 文档明说「MOSS-Image has no image input, so it cannot copy a reference」）；编辑模板用编辑 LoRA 才能接图，最多 4 张参考 `S12` |
| 布局 / 框控制 | **原生 JSON 布局**：`summary` + `items`（`label` / `caption` / `text` / `bbox`），`bbox_format: yxyx`，`bbox_coord_space: abs_centralized`（以画布中心为原点的像素坐标）`S11`；ComfyUI 有 `Create Bounding Boxes` + `MOSS-Image Layout Prompt` 模板 `S12` |
| 局部编辑（mask / 圈选 / 涂抹） | 无涂抹/圈选；官方路线是改 JSON 布局重排 + 编辑 LoRA（整帧重绘、有 CFG 4 与 `edit_format` 要求）；另有 MoGe 3 估几何 + 控制适配器 LoRA 做色/深度控制 `S11` `S12` |
| 风格参考 | **未见官方说明**（无图像输入）`S11` `S12` |
| 分辨率与步数 | 官方采样：50 步、CFG 3、shift 5、第 22 层负分支跳层；ComfyUI 模板 25 步 / CFG 2.5 / `er_sde`；建议 1–2 MP，外推 0.25–4 MP，边长 16 的倍数 `S11` `S12` |
| 显存门槛 | 官方实测 1024² 50 步：GPU 峰值 3.65 GiB（offload 模式）+ 系统内存 14 GiB；`auto` profile 在 GPU 预算 <12 GiB 时切 `minimal` `S11` |
| ComfyUI 支持 | 原生支持 2026-10-16 合并（commit `e087d8f6` 起，`v0.41.3` 不含）；文生图模板需 0.42.0+，编辑与色深模板需 0.43.0+；教程 <https://docs.comfy.org/tutorials/image/moss-image/moss-image> `S12` `S13` |
| 已知限制 | 无图像输入是最硬的限制；SFT 有竖向色带（社区 LoRA 缓解）；超 2 MP 需分段放大；prompt enhancer 会把布局框改写掉，布局模板默认关闭它 `S11` `S12` |

## 六、视频：MiniMax H3

官方接口 <https://platform.minimax.io/docs/api-reference/video-generation-v2-create>，模型族含 `MiniMax-H3`、`-Highspeed`、`-Lite`、`-Max` `S15`。本项目已接入 `MiniMax-H3` 与 `MiniMax-H3-Max` 两种意图：`endpoints`（首尾帧）与 `reference_motion`（参考生视频）。

| 项目 | 结论 |
| --- | --- |
| 两种意图 / 解决什么问题 | **首尾帧（i2v / fl2v）**：给首帧、尾帧或两者 + 文本，官方表述为「开头或结尾画面被完全控制」，适合让一张定稿静帧自然动起来或补一段过渡 `S14`。**参考生成（r2va）**：给参考图 / 视频 / 音频的任意组合 + 文本，保持参考主体的特征一致，可参考角色、动作、镜头、风格、声音或剪辑节奏 `S14` |
| 输入图要求（首尾帧） | 图片 0、1 或 2 张（首帧 ≤1、尾帧 ≤1）；单张 ≤30 MB；宽高各 ∈[256, 5760]；宽高比 ∈[0.4, 2.5]（即 2:5–5:2）；JPG/PNG/WEBP/HEIC/HEIF `S14` `S15`。尾帧必须与首帧配对出现 `S15` |
| 输入要求（参考生成） | 参考图 ≤9 张、参考视频 ≤3 段（每段 2–15 s、总长 ≤15 s、单个 ≤50 MB、H.264/H.265 + AAC/MP3、帧率 23.976–60）、参考音频 ≤3 段（每段 2–15 s、单个 ≤15 MB）；混合输入合计 ≤12 个文件；请求体 ≤64 MB `S14` `S15` |
| 互斥关系 | 一个请求里 `first_frame` / `last_frame` 与 `reference_image` / `reference_video` / `reference_audio` **不能混用** `S15` |
| 比例语义 | 首尾帧模式的 `ratio` 由输入图决定，恒为 `adaptive`，填别的具体比例不报错但会被忽略；参考生成模式 `ratio` 可选，缺省 `adaptive`；文生视频必须给具体比例 `S15`。本项目 `endpoints` 用 `adaptive`、`reference_motion` 用 `16:9` 与此一致 |
| 分辨率与时长 | `MiniMax-H3`：768P / 2K，4–15 s（整数）；`-Max`：480P / 768P（不支持 2K），5–15 s（不支持 4 s）；`-Highspeed` / `-Lite`：480P / 768P，4–15 s `S14` `S15` |
| 音频 | `generate_audio` 默认 `true`，输出带音轨；设 `false` 得静音视频 `S14` `S15` |
| 首帧是否严格一致 | 官方只写「开头或结尾画面被完全控制」，**未见官方说明**给出逐像素一致的保证或量化指标 `S14` |
| 失败与重试语义 | 异步任务：创建拿 `task_id`，轮询 `GET /v2/query/video_generation/{task_id}`；状态含 `queued` / `running` / `succeeded` / `failed` / `cancelled`，成功时 `task.content.url` 直接给下载地址；错误码 400 / 401 / 402 / 422 / 429 / 500；官方建议轮询间隔 10 s `S14` `S15`。**未见官方说明**自动重试或失败退款口径 |
| 成本（官方价目表） | 按输出秒计费：H3 2K $0.13/s、768P $0.08/s；H3-Max 768P $0.08/s、480P $0.05/s；输入视频按所选模型的输出分辨率另行计费（H3-Max 768P 输入 $0.143/s）；每任务前 5 张图免费（H3-Max 前 2 张），超出按张计费；音频输入免费 `S16` |
| 其它官方能力 | 768P→2K 的「视频再生」任务（按 task id 或源视频重跑，源视频须带音轨）；`H3-Context-IR` 只产出增强后的提示词、不出视频；`-Max` 额外支持 `extra.prompt_expansion_mode`（`disabled` / `balanced` / `quality`）`S14` `S15` |

### 与「风格化关键帧 → 视频」的衔接

1. 我们的风格帧（导演台定案后的关键帧风格图）当 `first_frame`：因为 `ratio` 恒为 `adaptive`，画幅由这张图决定，所以出图阶段就得定好画幅，不要指望接口再裁。
2. 同一镜若已排了下一关键帧，可把它当 `last_frame` 一起提交，得到首尾受控、中间由模型补的过渡；两端构图与主体必须真的能接上（同一场景、同一角色外观、同一光位），否则模型只能在两端之间「变形」，官方对两端不一致没有兜底说明 `S14`。
3. 参考片段从哪来：优先用我们自己的干净预演导出（无标注、无文字叠加的 2.5D 运镜预演 MP4），它天然带我们想要的机位与节奏；其次是项目内已采用的镜头成片截段。注意参考视频受 50 MB / 单段 2–15 s / 总计 15 s 限制 `S15`，预演要按段切好再传。
4. 另一种接法：`reference_motion` 用参考视频给动作与镜头、参考图给角色外观，这时风格帧不是首帧而是「外观参考」，首帧一致性就不再受控——两条路线的取舍要在镜头级明确写下来。

## 七、对本项目管线的适用性

| 步骤 | 建议模型 | 理由 |
| --- | --- | --- |
| 分镜 / 素描图稿（描述型素描） | **FLUX 3 Image Klein**（沿用现有 `storyboard_flux_klein.json` 工作流） | 唯一同时具备原生 bbox、≤10 张参考图、可跑 8 GB int8、且已在项目工作流里验证过的组合 `S2` `S4`；素描本质是「线稿 + 构图」，不需要强风格，Base/Distilled 都够 |
| 需要保留已有门墙结构时 | Klein 的简洁布局图 + 明确文字；或 MOSS-Image 的 JSON 布局 | 两者都能把「哪里有什么」写死；MOSS 的 bbox 结构最干净但它不吃图像输入，只能纯文字 + 布局 `S11` `S12` |
| 风格化关键帧（项目风格迁移） | **Krea 2 Turbo** 主选，Klein 备选 | Krea 2 的定位就是「风格参考 + 风格 LoRA」，9 个官方风格 LoRA 各带触发词，是四处里唯一把风格参考做成第一能力的 `S9` `S10`；Klein 备选的好处是素描与风格化共用一套权重，切换成本低 `S2` |
| 角色 / 资产一致性强的关键帧 | **Qwen-Image-2.1** | 官方强调最多 10 张参考图与人物、商品的身份保持，并给了「三视图角色参考出分镜」的示例 `S5`；但许可仅非商用，只能用于非商用验证 `S7` |
| 局部返修（改一只手、换一件衣服） | **Qwen-Image-2.1** 圈选/涂抹；或 Klein 的框行替换 | Qwen 是唯一官方支持圈选、涂抹与独立 mask 的 `S5` `S6`；Klein 用 `new` 行（`from: null` + `tgt_bbox`）也能替换指定区域，但框不是遮罩，边界约束更软 `S3` |
| 透明前景 / 抠主体素材 | Qwen-Image-2.1（原生 RGBA） | VAE 带四通道，透明是生成进 alpha 的，不是事后抠 `S5` `S6` |
| 版式重、文字多的画面（分镜表、字幕板、海报式画面） | MOSS-Image 或 Qwen-Image-2.1 | MOSS 主打中英长文本与 JSON 布局、提示词可到约 4K token `S11` `S12`；Qwen 原生 2K + 专业排版 `S6` |
| 关键帧 → 视频 | **MiniMax H3**（风格帧当首帧）/ H3-Max（更便宜更快，但上限 768P） | 见 §六：首尾帧受控、参考生成可借动作与镜头，且已有代码接入 `S14` `S15` `S16` |

### 预演导出的框布局（每个对象的 bbox + 深度序）怎么喂

| 模型 | 喂法 | 深度序怎么办 |
| --- | --- | --- |
| Klein | 每个对象一行 `{"id": "obj_1", "bbox": [top, left, bottom, right], "desc": "..."}`，坐标是 0–1000 的整数；caption 文本里用同一个 id 的尖括号标签指到该对象（如 `obj_1` → 尖括号包裹）；框只引导、不裁剪，元素可以略微溢出 `S3` | 官方没有「层序 / 深度」字段，**未见官方说明**框的先后是否影响遮挡；只能把前后关系写进 `desc` 与 caption（如「在…之后」「被…遮挡」），并在编辑时给必须不动的东西补 `keep` 行 `S3` |
| Qwen-Image-2.1 | 没有 bbox 通道：把 bbox 渲染成标注图（圈选 / 涂抹 / 色块）或单独 mask，作为参考图送进去，提示词里点名「红色区域」这类标记 `S5` `S6` | 同样没有深度字段；深度只能靠提示词描述与分区多次编辑累积，一次只改一块更稳 `S6` |
| MOSS-Image | 直接吃 JSON 布局：`summary` + `items[]`（`label` / `caption` / `text` / `bbox`），`bbox_format: yxyx`，`bbox_coord_space: abs_centralized`（画布中心为原点，1024 画布每轴 −512…+512），坐标必须与出图尺寸一致 `S11` | 无深度字段；ComfyUI 模板按「先画背景、后画前景」的顺序出框 `S12`，顺序只影响框的排列，**未见官方说明**它等价于遮挡顺序 |
| Krea 2 | 无 bbox、无 mask 通道，只有文字与风格参考图 `S9` `S10` | 只能写进提示词 |

### 灰模渲染图当参考图为什么收益低

这是我们的判断，不是官方结论：

1. 四个模型的参考图通道都是为「外观与身份」设计的（`ref_image_*`、风格参考、参考主体），喂进去的灰模提供的是几何，而几何本来就已经被 bbox / 布局 / 线稿表达了，属于重复信息 `S2` `S9` `S11`。
2. 灰模的灰调、无材质、塑料高光会被当成风格吸收。Krea 2 的风格参考与 Qwen 的参考图都直接影响成片观感，灰模会把结果往「未完成渲染稿」拉 `S9` `S5`。
3. 参考槽位是稀缺资源（Klein 10 张、Qwen 10 张、Krea 本地 1–2 张），塞灰模就挤掉了角色卡 / 场景卡 / 门墙结构图这些真正带外观信息的东西 `S2` `S5` `S9`。
4. 我们自己的素描口径也把几何轮廓当内部参考，最终绑定的是服务返回的描述型素描（见 [STORYBOARD_STYLE_PROVIDER.md](screenplay/STORYBOARD_STYLE_PROVIDER.md)）；即几何只该进 bbox 与文字，不该进图像通道。

## 八、风险与待验证项

| 风险 | 说明 |
| --- | --- |
| 厂商自述对比分 | BFL 的 Elo 曲线写的是「内部评测」`S2`；Qwen 的 Qwen-Image-Bench 是自家基准 `S5`；Krea 引的 Artificial Analysis 排名也由厂商转述 `S8`。这些分数不能横向拼成一张榜，选型要以自己在项目素材上的分组测试为准 |
| 版本门槛 | MOSS-Image 要求最狠：文生图 0.42.0+，编辑 / 色深 0.43.0+，而 0.41.3 还不含支持 `S12` `S13`。Klein 要 0.41.0+ `S4`。Qwen 与 Krea 官方教程未标版本号，实测前先整体升级到每夜版 `S6` `S9`。同一台机器上四种模型共存，ComfyUI 版本取最高要求 |
| 显存与速度 | Klein bf16 31 GB 级、fp8 26 GB、int8 可到 8 GB；MOSS 靠 offload 只要 3.65 GiB 显存但要 14 GiB 系统内存；Qwen 与 Krea 官方未给门槛 `S2` `S11` `S5` `S8`。视频侧按秒计费，试错成本直接跟时长挂钩 `S16` |
| 许可（必须逐条确认才能商用） | **Qwen-Image-2.1 明确仅非商用**（Qwen Research License，商用需另签）`S7`；**Krea 2** 社区许可允许商用但限公司年营收 <100 万美元，且有内容过滤与 AI 披露义务 `S9`；**FLUX 3 Image Klein** 权重为 FLUX Kommunity License，代码 Apache 2.0，条款正文在权重仓库的 LICENSE.md，本次未逐条核对完营收门槛与产出物条款，**待确认** `S2`；**MOSS-Image** 仓库只明确「管道代码 Apache 2.0」，ComfyUI 官方教程写权重 Apache 2.0，两者口径不完全一致，**权重许可待确认** `S11` `S12`；**MiniMax H3** 的商用与服务条款**未见官方说明**（价目表不等于授权条款）`S16` |
| 待验证 | 1) Klein 的框行在「多对象 + 强遮挡」场景下的贴合率；2) 深度序是否有任何官方字段可表达（四个模型都未见）；3) 灰模 vs 线稿 vs 无参考图三组 A/B，量化素描贴合度；4) Krea 2 风格参考在我们项目风格上的迁移强度与风格 LoRA 触发词副作用；5) Qwen 圈选在简单场景之外的稳定性（项目内已有「未显示优于色块」的旧结论）；6) MiniMax H3 首帧实际一致性（用同一风格帧重复提交测方差）；7) 四种模型的许可证逐条法务确认 |

## 九、来源

| 编号 | 来源 |
| --- | --- |
| `S1` | FLUX 3 Image Klein 官方模型文档（规格表：5.6B、4K、16 / 50 步、bbox 支持）<https://docs.bfl.ai/flux_3/flux3_image_klein> |
| `S2` | FLUX 3 Image Klein 官方推理代码与权重说明（变体表、许可证、显存实测、upsampler、bbox JSON 格式）<https://github.com/black-forest-labs/flux-3-image-klein> |
| `S3` | 官方边界框教程（`[top,left,bottom,right]` 0–1000、keep/move/new/remove 行、框不裁剪、小框限制）<https://docs.bfl.ai/flux_3/flux3_image_bounding_boxes> |
| `S4` | ComfyUI 官方教程：FLUX 3 Image Klein（0.41.0+、模型文件、模板与控制项）<https://docs.comfy.org/tutorials/flux/flux-3-klein> |
| `S5` | Qwen-Image-2.1 官方仓库 README（架构、10 张参考图、圈选/涂抹/mask、RGBA、2K、步数）<https://github.com/QwenLM/Qwen-Image-2.1> |
| `S6` | ComfyUI 官方教程：Qwen-Image-2.1（16 槽编码、Paint Pen 局部编辑、采样设置、VAE 伪影、PE 增强器）<https://docs.comfy.org/zh/tutorials/image/qwen/qwen-image-2-1> |
| `S7` | Qwen Research License Agreement 正文（仅非商用）<https://github.com/QwenLM/Qwen-Image-2.1/blob/main/LICENSE> |
| `S8` | Krea 2 官方仓库（RAW 52 步 / Turbo 8 步、1K–2K、许可指向）<https://github.com/krea-ai/krea-2> |
| `S9` | ComfyUI 官方教程：Krea-2（RAW / Turbo、风格 LoRA 与触发词、风格参考工作流、1K–2K）<https://docs.comfy.org/tutorials/image/krea/krea-2> |
| `S10` | ComfyUI 官方文档：Krea 2 托管节点（Medium / Large、≤10 张风格参考、moodboard）<https://docs.comfy.org/tutorials/partner-nodes/krea/krea-2-t2i> |
| `S11` | MOSS-Image 官方仓库（5B 单流 DiT、JSON 布局与坐标空间、采样设置、显存实测、Apache 2.0 代码）<https://github.com/OpenMOSS/MOSS-Image> |
| `S12` | ComfyUI 官方教程：MOSS-Image（0.42.0 / 0.43.0 门槛、无图像输入、布局模板、编辑 LoRA、MoGe 3 控制）<https://docs.comfy.org/tutorials/image/moss-image/moss-image> |
| `S13` | MOSS-Image 官方 ComfyUI 说明（原生支持自 2026-10-16 合并，`v0.41.3` 不含）<https://github.com/OpenMOSS/MOSS-Image/blob/main/comfyui/README.md> |
| `S14` | MiniMax 官方视频生成指南（四种模式、输入条件表、文件与时长限制、异步流程、音轨开关）<https://platform.minimax.io/docs/guides/video-generation> |
| `S15` | MiniMax 官方 Create Video Generation Task 接口（模型族、`role` 语义、互斥关系、`ratio` 规则、时长与分辨率、错误码）<https://platform.minimax.io/docs/api-reference/video-generation-v2-create> |
| `S16` | MiniMax 官方价目表（按输出秒计费、图片免费额度、视频再生与 Context-IR 价格）<https://platform.minimax.io/docs/pricing/overview> |
