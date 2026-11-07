# 空间布局约束生图测试报告

日期：2026年11月7日。对象：ChatGPT 内置生图、FLUX.3 Image Klein、Qwen Image 2.1。

本次测试研究如何把三维预演中已经确定的位置与尺度传给生图模型，同时避免传入错误人偶姿态。三轮共生成16张原始图。结果表明，输入形式对这组样本影响明显：FLUX 原生区域表最贴近指定人物二维布局；通用布局图配合明确文字也有改善；Qwen 圈选编辑能识别人物与清除标记，但后景人物尺度仍偏离要求。二维贴合不能证明三维空间或摄影机准确，左图中门所属墙面的错误就是实例。

## 测试范围与请求数量

| 轮次 | 输入方式 | ChatGPT | FLUX | Qwen | 合计 |
|---|---|---:|---:|---:|---:|
| 第一轮 | 透视网格、落点与身高立杆；通用文字 | 2 | 2 | 2 | 6 |
| 第二轮 | 淡色占位区；明确左右、脚底位置与比例文字 | 2 | 2 | 2 | 6 |
| 第三轮 | FLUX 原生区域表；Qwen 圈选标注编辑 | 0 | 2 | 2 | 4 |
| 合计 | 每种方式均含原布局与水平镜像 | 4 | 6 | 6 | 16 |

每个条件只生成一次，不重试、不挑选最佳结果、不修图。16次实际生成均完成，但完成不等于空间约束通过。测试未修改叙事工程业务数据，原始图、实际提示词、工作流和服务回执均保留。

## 场景与目标

场景为一个简单房间、两位成人、一位儿童、一张空桌和一扇门。人物用衣色区分：A为红衣成人，B为蓝衣成人，C为黄衣儿童。A与B实际身高均为1.8米，C为1.2米；A和C离摄影机3米，B离摄影机6米。人物的动作只要求说话、倾听与观看，不预先指定四肢姿态。

第一轮通过针孔投影生成空间图，水平镜像得到第二个条件；相机及坐标保存在[原始几何记录](../artifacts/spatial-reference-test-20261107/geometry.json)。后两轮沿用同一人物分配区域，按1280×736画布表达。原布局横向顺序为A—B—C，镜像为C—B—A；B在后景，其投影身高应约为A的一半；C在近景，其投影身高应约为A的三分之二。

| 对象 | 原布局脚底坐标 | 镜像脚底坐标 | 目标画面高度 |
|---|---|---|---|
| A | (0.203, 0.772) | (0.797, 0.772) | 基准1.00 |
| B | (0.719, 0.568) | (0.281, 0.568) | 约0.50 |
| C | (0.789, 0.772) | (0.211, 0.772) | 约0.67 |

坐标从画面左上角起，分别按宽、高归一化。门随镜像由左后方变为右后方，桌子保持居中。位置和比例是设计依据；身高线、色块与椭圆均为辅助标记，要求在成图中消除。

## 实际运行配置

| 项目 | ChatGPT | FLUX.3 Image Klein | Qwen Image 2.1 |
|---|---|---|---|
| 调用入口 | 当前聊天的内置生图工具 | 本机ComfyUI | 本机ComfyUI |
| 实际版本 | 工具未公开底层型号 | 普通蒸馏版INT8 convrot，非base | 普通版INT8 convrot，非Turbo或云端Pro |
| 采样步数 | 不可设置或读取 | 第一、二轮8；第三轮16 | 第一、二轮25；第三轮40 |
| 其他采样参数 | 种子、采样器未公开 | CFG1、Euler/simple、seed424242 | CFG1、Euler/simple、seed424242 |
| 输出尺寸 | 第一轮1672×941；第二轮1654×951 | 1280×736 | 1280×736 |

本地运行记录为ComfyUI 0.43.0、RTX 4070 Ti SUPER，启动参数包含`--fast fp16_accumulation`。FLUX使用`qwen3vl_4b_int8_convrot`编码器与`flux2-vae`；Qwen使用`qwen3vl_8b_int8_convrot`编码器与专用2.1 VAE。不同量化和版本不纳入同一结论。

第一轮参考图1280×720，Qwen输入上下各补8像素白边，无拉伸；FLUX使用原始参考。后两轮参考图均为1280×736。ChatGPT实际输出尺寸由工具决定，观察时使用归一化坐标。ChatGPT在同一聊天内顺序生成，不能保证独立上下文；不能将它标为某个已知API型号。各服务未提供本轮费用或token用量，记录为未知。

## 各轮测试方法

### 第一轮 图中提供空间关系

参考图包含地面透视网格、人物脚底落点、直立身高线、身份字母，以及门和桌子的线框。原布局与镜像图使用同一份英文提示，文字只解释标记含义，不说明具体左右、坐标或距离。因此镜像差异主要由输入图传递。

提示要求生成简洁线描，保持参考图空间关系，选择自然姿态，并清除网格、标签、立杆和落点。FLUX使用参考潜变量方式，Qwen通过`TextEncodeQwenImage21`输入参考图，均未使用FLUX原生区域表。

输入：[原布局](../artifacts/spatial-reference-test-20261107/layout-left.png)、[镜像布局](../artifacts/spatial-reference-test-20261107/layout-right.png)、[完整提示词](../artifacts/spatial-reference-test-20261107/prompt.txt)。

### 第二轮 简洁布局图加明确文字

去掉网格、标题和立杆，用淡色矩形标出人物画面占用范围；保留简单墙线、门和桌子。提示词分段说明身份、空间、脚底坐标、动作、风格与标记清除。原图和镜像图各有对应文字，明确左右、前后、空距、画面高度比，并要求保持取景、不拉近后景人物。

例如文字明确“后景成人约为前景成人一半高”，同时提供脚底的归一化位置。由于图与文字同时改变，且文字已包含布局信息，这轮衡量的是联合形式的实用效果，不能声称单独证明读图能力提升。

输入：[原布局](../artifacts/spatial-reference-test-20261107-round2/layout-left.png)、[镜像布局](../artifacts/spatial-reference-test-20261107-round2/layout-right.png)、[原布局提示词](../artifacts/spatial-reference-test-20261107-round2/prompt-left.txt)、[镜像提示词](../artifacts/spatial-reference-test-20261107-round2/prompt-right.txt)。本地采样参数保持第一轮设置。

### 第三轮 分别采用已确认的模型输入形式

**FLUX：原生区域表，纯文生图。** 正文通过`<obj_1>`等标签关联对象，末尾附`id、bbox、desc`列表。边界框按0—1000归一化，顺序为`[y_min,x_min,y_max,x_max]`。三个人、门、桌子的区域由同一布局程序转换；不再向FLUX输入空间参考图。该格式依据[官方布局说明](https://huggingface.co/blog/flux-3)。

原布局中的红衣成人条目为：

```json
{"id":"obj_1","bbox":[283,156,772,250],"desc":"Full-body adult in a red jacket, standing in the foreground, talking with a modest gesture toward the blue-jacket adult."}
```

本机`Flux3LayoutPrompt`节点的作用是拼接该字符串。本轮服务把直接输入的列表视为节点连线，因此采用与其实际源码核对一致的最终序列化字符串，未直接执行该节点。记录中明确区分“使用模型原生格式”和“执行特定界面节点”，没有声称调用未发生的节点。[实际提示](../artifacts/spatial-reference-test-20261107-round3/flux-serialized-left.txt)、[区域表](../artifacts/spatial-reference-test-20261107-round3/flux-regions-left.json)、[序列化核对](../artifacts/spatial-reference-test-20261107-round3/flux-native-serialization-check.json)。

**Qwen：圈选标注编辑。** 官方确认支持圈选、涂画或独立蒙版局部编辑；本轮选择圈选方式，在原场景图中用不同颜色椭圆指定三个人物区域，中文指令要求以自然人物替换这些区域，遵循上下边界，保留场景并消除标记。[官方说明](https://github.com/QwenLM/Qwen-Image-2.1)

Qwen通过`TextEncodeQwenImage21`接收单张圈选参考，未添加采样噪声蒙版、区域插件或输出合成。本机入口没有单独的bbox参数，本轮也未核实到与FLUX相同的Qwen边界框协议。因此结果只能评价圈选方式，不能推广到尚未确认的其他布局协议。[实际圈选图](../artifacts/spatial-reference-test-20261107-round3/qwen-annotated-left.png)、[实际指令](../artifacts/spatial-reference-test-20261107-round3/qwen-prompt-left.txt)。

第三轮采样改为FLUX16步、Qwen40步，并使用不同输入路线和提示语言。与前两轮的差异不能唯一归因于布局语法，两个模型之间也不是严格的同输入排名。

## 评价方法

对原始图目视检查：人物数量与身份、横向顺序、前后关系、脚底位置、画面尺度、门桌布局、标记残留，以及是否新增物件。评价由主代理独立完成；模型生成完成状态与空间要求满足分别记录。

第二、三轮还近似标注人物头顶与脚底，计算投影高度比和脚底归一化误差。头发、鞋底和姿态会带来人工观察误差，这不是自动人体检测或三维测量。观察前记录的探索参考范围是：脚底每轴误差不超过0.06，B/A高度比0.4—0.6，C/A为0.55—0.8。接近边界的点有不确定性，这些范围不代表专业制作标准。

最大单轴误差取所有人物的`|u实际−u目标|`及`|v实际−v目标|`中的最大值；宽、高分别归一化，并非三维距离或欧氏误差。第一轮未预设相同阈值，不与后两轮混算统一通过率。

## 测试结果

### 第一轮

| 模型 | 原布局 | 镜像布局 | 标记残留 |
|---|---|---|---|
| ChatGPT | 横向顺序正确；后景B前移并放大 | 跟随镜像，但位置和比例漂移 | 两张均保留网格 |
| FLUX | B与C左右及前后关系错误，桌上新增瓶子 | A仍在左侧，人物集中于中部 | 两张均保留网格 |
| Qwen | 大体左右关系正确，B偏大偏前 | A仍在左侧，B缩小得像儿童并与A重叠 | 左图基本清除，右图残留网格 |

本轮ChatGPT更能跟随图中的左右变化，但三者都未完整服从空间要求。该观察只针对通用图文输入，不代表原生控制能力上限。[第一轮完整对照](../artifacts/spatial-reference-test-20261107/comparison.html)

### 第二轮

六张均跟随人物横向镜像，且没有明显的布局标记残留。FLUX两张的位置和比例更接近这组要求，ChatGPT把B缩得过小，Qwen仍将B画得偏大偏前。

| 模型 | B/A高度比 原图／镜像 | C/A高度比 原图／镜像 |
|---|---|---|
| 目标 | 约0.50／0.50 | 约0.67／0.67 |
| ChatGPT | 0.33／0.31 | 0.71／0.72 |
| FLUX | 0.57／0.54 | 0.64／0.69 |
| Qwen | 0.72／0.75 | 0.63／0.63 |

这些人工近似测量支持“形式改变后，这组样本的大体布局改善”，不能单独证明图像理解或某个文字策略的效果。[第二轮原图与前轮对照](../artifacts/spatial-reference-test-20261107-round2/comparison.html)

### 第三轮

| 路线 | B/A高度比 原图／镜像 | C/A高度比 原图／镜像 | 脚底最大单轴误差 原图／镜像 |
|---|---|---|---|
| FLUX原生区域表 | 0.51／0.50 | 0.67／0.67 | 约1.0%／1.3% |
| Qwen圈选编辑 | 0.82／0.69 | 0.65／0.56 | 约12.0%／9.9% |

FLUX的人物二维布局非常接近这组目标，且没有布局文本或辅助符号混入成图；但原图把要求的后墙门画成侧墙门。这表明边界框不能独立表达完整三维结构。Qwen两图左右关系正确、圈选标记消除，B的大小及落点仍明显偏离目标。[第三轮完整对照](../artifacts/spatial-reference-test-20261107-round3/comparison.html)

![FLUX第三轮原布局结果](../artifacts/spatial-reference-test-20261107-round3/flux-left.png)

上图人物布局贴近目标，但左侧门的墙面归属错误。

![Qwen第三轮原布局结果](../artifacts/spatial-reference-test-20261107-round3/qwen-left.png)

上图后景蓝衣成人未维持目标投影高度和落点。

## 结论与接入建议

优先保留中立的空间与摄影机设计数据，由程序转换为具体模型支持的输入。对已验证的FLUX路线，直接生成对象标签、区域表和必要语义描述，比要求模型从带辅助线的图中自行分离信息更适合后续接入。通用参考图仍可作为其他模型的可选辅助形式。

布局表只约束二维区域，不能替代深度、墙面归属、具体姿态、接触和镜头语言。粗糙人偶或示意动作继续留在预演端，出图依据只包含明确设计的空间与姿态；生成后还需检查是否偏离。不能将两张结果视为像素级保证，或据此对三个模型做总体排名。

正式接入前，应再覆盖不同景别、俯仰机位、复杂遮挡、区域重叠、不同身高和更多随机种子。若要确定布局格式本身的收益，应固定步数、提示语义与种子，设置无图文字、通用布局图和原生布局表的对照组。本报告没有执行这些扩展测试。

## 证据与复现入口

| 记录 | 第一轮 | 第二轮 | 第三轮 |
|---|---|---|---|
| 测试设计 | [protocol](../artifacts/spatial-reference-test-20261107/protocol.json) | [protocol](../artifacts/spatial-reference-test-20261107-round2/protocol.json) | [protocol](../artifacts/spatial-reference-test-20261107-round3/protocol.json) |
| 本地真实参数及原始回执索引 | [summary](../artifacts/spatial-reference-test-20261107/comfy-test-summary.json) | [summary](../artifacts/spatial-reference-test-20261107-round2/comfy-test-summary.json) | [summary](../artifacts/spatial-reference-test-20261107-round3/comfy-test-summary.json) |
| ChatGPT记录 | [记录](../artifacts/spatial-reference-test-20261107/chatgpt-results.json) | [记录](../artifacts/spatial-reference-test-20261107-round2/chatgpt-results.json) | 未调用 |
| 人工观察与测量 | [观察](../artifacts/spatial-reference-test-20261107/chatgpt-assessment.json) | [assessment](../artifacts/spatial-reference-test-20261107-round2/assessment.json) | [assessment](../artifacts/spatial-reference-test-20261107-round3/assessment.json) |
| 本地调用脚本 | [脚本](../artifacts/spatial-reference-test-20261107/run-comfy-test.py) | [脚本](../artifacts/spatial-reference-test-20261107-round2/run-comfy-test.py) | [脚本](../artifacts/spatial-reference-test-20261107-round3/run-comfy-test.py) |

脚本保留防重复提交标记；复验需另建隔离结果目录，不删除原标记来重复旧轮。第三轮的本地预检曾因上传前读取动态图片列表而断言失败，当时生成请求数为0；刷新上传后的节点信息后才提交4次实际生成，诊断已保存，没有重试失败模型请求。检查器问题不算模型质量失败。

所有历史分轮报告和原图保持原样。本报告只汇总既有证据，没有新增生图请求。
