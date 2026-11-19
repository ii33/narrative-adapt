# 本仓库上的补丁清单（narrative-adapt / 预演接口层）

DirectorDesk 是 MIT 授权、原样 vendored 进来的（`.runtime/vendor/director-desk/`，不进 git）。
这份文件只记录**我们相对上游加了什么、为什么**；接口契约在 `docs/PREVIS_HOST_API.md`，
两边要一起看。补丁纪律：只加接口，不改它原有的界面行为。

## 改动的文件

| 文件 | 类型 | 目的 |
|---|---|---|
| `src/host/host-api.ts` | 新增 | 接口本体：把 `window.__narrativePrevisHost` 挂上去，实现契约里的全部方法、离屏抓帧、`onChange` 通知、`applyBatch` 事务编排；v2 追加 `setReadability`（见下文）。 |
| `src/host/host-scene.ts` | 新增 | 场景层：`Project` ↔ 契约 JSON 的翻译，以及每一类写操作的校验与语义（新增／修改／删除／改机位／设切镜）。 |
| `src/readability.ts` | 新增 | 可读性层：按 kind 配色、房间/场地轮廓与尺寸、人物头顶标志与朝向箭头、「拍摄」视角的 3D 标签（含字号与避让）。全部可一键撤除。 |
| `src/engine.ts` | 改 | ① `readability` 字段并挂进场景；② `rebuild()` 末尾重建可读性层（在 `sample()` 之后，包围盒要按已应用的姿态量）；③ `sample()` 里人物着色改走 `humanColor()` 并每帧跟随标志；④ `prepareView()` 里按视角决定可见性、导出时整层关掉；⑤ `setReadability()` 公开入口；⑥ `getProjectedLabels()` 扩到所有实体，并按距离给字号、向上错开重叠。 |
| `src/startup.ts` | 改 2 行 | `import` + 调 `installPrevisHostApi()`。放在 `mountLocalization()` 之后、`import('./main.ts')` 之前：接口壳要在三维引擎起来之前就在，`hello()` 才能随时回话。 |
| `src/main.ts` | 改 3 处 | ① 两个 import；② 新增 `previsCommit()`——接口的事务入口，和 `change()` 同一套历史／校验语义，但把失败原因抛回去而不是只弹 toast；③ 页面装配完成后 `bindPrevisHostRuntime({...})`，把 `project / engine / sceneId / revision / blockedReason / commit / seek` 交给接口层。 |
| `src/main.ts`（v2） | 改标签渲染 | `engine.onFrame` 里给 `.object-label` 写 `font-size` / `opacity`：字号来自 `getProjectedLabels()` 的距离换算，挤不开的标签半透明让位。只动我们自己的标签节点，没有改样式表。 |
| `src/scenes/scene-workspace.ts` | 改 1 个签名 | `commit(project, label = '编辑戏段')` 多收一个标签，转给 `SceneSession.commit`。这样助手铺完场景后，撤销按钮的提示会写成「接口批量修改 6 条」，而不是笼统的「编辑戏段」。默认值保持原样，原有调用方一行不动。 |
| `src/storage.ts` | 改 1 行类型 | `EditorHistory.commit(project, label?)` 跟上上面的签名（`SceneWorkspace implements EditorHistory`）。 |
| `PATCHES.md` | 新增 | 就是本文件。 |

没有改动：任何 `*.css`、`index.html`、`vite.config.mjs`、`ui/` 下的面板与交互、`assets/` 下的白模构建。
换言之页面的可见行为、按钮、快捷键、布局都没变——唯一的例外是新增事务会出现在撤销栈里（这本来就是用户按 Ctrl+Z 应当退掉的一次编辑）；
可读性层是新加的一层显示，默认全开，`setReadability({all:false})` 后与上游完全一致。
（注：`package.json` 在 `git status` 里显示为已修改，那是 vendoring 时的 LF→CRLF 行尾差异，不是这次改的。）

## 接口实现的几个决定（契约没写死的地方）

- **`captureFrame` 的返回格式**：`{ok, png_base64, width, height, cameraId, time_ms}`，其中 `png_base64`
  是**纯 base64，不含 `data:image/png;base64,` 前缀**（契约字段名就叫 `png_base64`，前缀由调用方按需拼）。
- **`captureFrame` 怎么渲染**：走页面自己的输出管线 `Engine.renderOutput()`（同一个 `shotRenderer`、
  同样的材质／阴影缓存／光效／墙面透传），先 `prepareOutput()` 把视频贴图备好，再在同一个 tick 里
  渲染并 `toDataURL('image/png')`。**不新建第二个 WebGLRenderer**：另起一套会得到一张“跟预览不一样”的图，
  就失去确认图的意义。抓帧结束后立刻把 `engine.time` 与取景画布尺寸恢复（`sample(previous)` +
  `requestResize()` + `render()`），播放状态（`playing`、播放头）不受影响。冒烟脚本用 `#timecode` 的
  文本前后一致来证明这一点。
- **`listCameras()` 直接返回数组**（`[{id, name, position, target, focal, ...}]`），因为契约的返回列写的
  就是数组；失败时才返回 `{ok:false, error}`。其余方法在成功时返回 `{ok:true, ...}`，失败返回
  `{ok:false, error}`——`error` 键始终存在，契约要求的形状不变。
- **时间单位**：接口进来出去一律毫秒（`time_ms` / `start_ms` / `end_ms` / `duration_ms`），页面内部一律秒，
  换算只发生在 `host-scene.ts` 的边界上。
- **`applyBatch` 的事务**：`clone(project)` 出一份副本 → 逐条 `applyOperation`（每条都校验 id 是否存在、
  字段是否合法、资产类型是否匹配）→ 全过之后一次 `commit()`（页面侧再 `assertProject` 全量校验一次）→
  失败则整份副本丢弃，报「第 N 条（op）失败，整批未生效：原因」。因为改的是副本、提交只有一次 `rebuild()`，
  所以不存在中间态重绘，也不存在“改了一半”的场景。
- **`onChange` 的触发点**是页面的 `revision` 计数器（每帧比一次），不是在接口方法里硬挂：这样用户在页面上
  手动拖对象、按撤销，也会照常通知主程序；同时天然节流到每帧最多一次。事件体
  `{sceneId, revision, entityCount, duration_ms, time_ms, source: 'api'|'ui'}`。
- **`setCamera(id, spec)` 的语义**：`position` 写机位站位，`target` 写静态瞄准点（同时把 `targetId`
  清空、`mode` 置 `free`——否则“跟随目标”会盖掉你给的瞄准点），`focal` 写毫米数（并删掉焦距动画通道，
  否则静态焦距不生效，这与页面里重新构图的做法一致）。`framing` 只是构图预设（全景／中景／近景／特写），
  只改 `targetHeight` 与焦距，不会自己算距离——“机位由调用方给”是这一层的约定。
  若该机位原来有路径（关键帧位置），**`setCamera` 会清掉路径并在返回值里带 `pathCleared: true`**，
  否则新位置根本不会生效。
- **`removeEntity`**：删机位时把切镜表整体改到另一台机位上（返回值 `cutsRemapped` 给出被改的时间点）；
  被删对象若是别的机位的瞄准目标，机位改成固定瞄准它最后的位置；手持绑定／结构连接的引用**不猜**，
  直接报错让调用方先解绑（那些关系要按引擎里的实时姿态解除）。
- **`updateEntity` 不允许改 `kind`**（改类型会让资产、动作、姿态全部失配），要换类型请删了重加。
- **`addEntity` 的人物会带一段覆盖整场的 `idle` 动作**、颜色按人物数量轮转，跟页面上的“添加资产”一致；
  缺省资产：`actor → person`、`prop → shape-box`（1 米方块，可用 `scale` 拉成墙／门／家具占位）、`camera → camera`。

## 可读性层（v2 补丁：`src/readability.ts` + `setReadability`）

预演场景是**纯盒子**搭的：白模墙、盒式家具、白模人物，机位固定。盒子本身看得懂「有几件东西」，
但看不出「哪个是人物」「他朝哪边」「这间屋子多大」。这一层只解决这三件事，不改场景数据。

### 上游已有什么 / 我们补了什么（动手前先查过）

| 元素 | 上游现状 | 这次补的 |
|---|---|---|
| 实体标签 | `#object-labels` DOM 浮层，**只有 actor 与 camera**，常显不等 hover，字号固定 9px，没有避让；只在布景视图；不会进抓帧 PNG | 扩到所有实体；字号按距离缩放并压到可读下限；投影重叠时向上错开、错不开就半透明；「拍摄」视角另有一份 **3D sprite 标签**（DOM 进不了 PNG）；贴地平板（地面等）不标，免得和尺寸标题挤在一起 |
| 地面网格 | `T.GridHelper(20, 20)`＝1 米一格，`engine.gridVisible` 默认 true，工具条「米格」可切；挂在 helpers（layer 1）上，**只出现在布景视图** | 不动它。抓帧/拍摄视角里另画一份 1 米线（细、半透明），位置与轮廓对齐 |
| 房间轮廓 | `makeRoom()` 只有实体墙/地面/天花板盒子，没有轮廓线；`#room-badge` 显示 w×d；`project.room` 默认关闭，且**接口 v1 没有改房间的操作** | 轮廓线（地面圈 + 顶部圈 + 四根竖线，扁盒子画法，1 像素线宽在抓帧里太细）＋地面 1 米网格＋尺寸标题。`room.enabled` 时用工程里的房间尺寸；没开房间时**按可见道具的实际占地量一遍**（纯盒子场景靠这条才有「场地 6 × 4 m」） |
| 实体配色 | `e.color` 按实体（人物从 `COLORS` 轮转），道具/墙在建模时用 `e.color` 或固定色；没有按 kind 的配色 | 「可读性模式」下按 kind 覆盖：墙地 `#cfc9bd`、家具 `#9db2bb`、人物 `#d79a5f`、机位 `#8e9cc4`。只改材质颜色并记住原色，关掉立即复原；`e.color`、工程文件、资产本身都不动 |
| 人物可辨识 | 白模人物鼻尖有个小凸起表示正面（`human-legacy.ts` 注释写明），缩略尺寸下看不出来；没有头顶标志 | 头顶琥珀色八面体 + 脚下扁三角箭头（朝向取模型根节点的 `rotation.y`：路径转身、`faceTarget` 都已经在里面）。标志 `depthTest:false`，被家具挡住也看得见 |
| 机位取景 | 拍摄面板就是真实输出管线（画幅遮幅、`#shot-lens`、`#shot-warning`），景别预设 全景/中景/近景/特写 会按焦距反算距离 | 不动 |

### 接口

```ts
window.__narrativePrevisHost.setReadability(options) -> {ok, applied}
// options = {persons?, roomOutlines?, kindColors?, labels?} ，另有 {all: true|false} 一次全开/全关
// 默认四项全开；具体的键优先于 all（{all:false, labels:true} = 只留标签）
// applied 回的是生效后的四项取值，例如 {persons:true, roomOutlines:true, kindColors:true, labels:true}
```

- **只改显示**：不进撤销栈、不触发 `onChange`、不写工程文件、不改机位与实体；关掉即回到上游观感。
  状态在页面内存里，刷新后回到默认全开。
- **抓帧里可见**：`captureFrame` 走 `engine.renderOutput`（拍摄机位 + shotRenderer），所以这一层的几何在
  layer 0 上、抓帧里就在；DOM 标签则只在布景视图里出现。两者名单一致（同一份 `labelTops` 与「贴地不标」规则）。
- **不进成片**：`engine.exporting` 为真时整层 `visible = false`，导出的视频里不会出现标注。
- **扩到 DOM 标签的代价**：`.object-label` 是页面原有、可点击选中的交互元素，我们没有替换它，只是补了
  `font-size`/`opacity` 两个内联样式（没有改任何 CSS 文件）。

### 冒烟

`.runtime/spike/previs_readability_smoke.py`（一次性脚本，不入库）：QtWebEngine 加载 dist →
`hello / getScene / applyBatch`（清掉模板摆件、铺 6×4 盒子房间 + 两个人物 + 两台机位）→
`setReadability({all:false})` 抓帧 → `setReadability()` 全开再抓同一机位 → 斜俯视再抓一张 →
核对「开关不改场景数据」与布景视角的 DOM 标签数量/字号，三张 PNG 写到 `.runtime/spike/`。

## 重新构建 dist

`node_modules` 已在仓库里（pnpm 布局），esbuild 二进制可用时直接构建即可：

```sh
cd .runtime/vendor/director-desk
node node_modules/vite/bin/vite.js build      # 等价于 pnpm exec vite build
```

如果 esbuild 报“binary not found”，先跑 `npx pnpm rebuild esbuild`（registry 走 npmmirror，已缓存）。
产物落在 `dist/`（v2 补丁后 6,415,610 字节 ≈ 6.4 MB，其中 engine 分包 592 KB，比打补丁前 +11 KB），由
`src/narrative/previs_workspace.py` 按
`NARRATIVE_PREVIS_DIST` → `<工程>/previs-web/` → `.runtime/vendor/director-desk/dist` 的顺序查找。
只跑 `vite build`（不跑 `npm run build`）可以跳过 `tsc --noEmit` 与发布脚本，接口补丁不需要那些。

## 宿主侧引导

`src/narrative/qml/DirectorWorkspaceView.qml` 里加了一个 `hostBootstrap` 字符串，在**主题脚本注入之后**
`runJavaScript(view.hostBootstrap, ...)`，然后再回调 `onReady`——既有顺序（先主题 CSS/JS、再 onReady）
一行没动，只是中间多了一步：确认 `window.__narrativePrevisHost` 在，并调用它的 `boot()`。
页面还没起来时它会放一个占位对象（`boot()/hello()` 返回可读的 `{ok:false, error:'预演接口未加载'}`），
主程序拿到的永远不是 `undefined`。

## 冒烟

`.runtime/spike/previs_host_smoke.py`（一次性脚本，不入库）：QtWebEngine 加载 dist →
`hello / getScene / addEntity(墙) / listCameras / setCamera / setTime / captureFrame / applyBatch(成功与失败各一次) / onChange`，
并把抓帧写到 `.runtime/spike/previs-frame.png`。跑法：

```sh
.venv\Scripts\python.exe .runtime\spike\previs_host_smoke.py
.venv\Scripts\python.exe .runtime\spike\previs_readability_smoke.py   # 可读性层的三张确认图
```
