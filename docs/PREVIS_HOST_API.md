# 预演页与主程序之间的接口层（v1）

预演页内嵌 DirectorDesk。主程序（Python 侧的工具、助手、界面）不直接读写 `.director` 文件，
而是通过注入到页面里的一个 JS 接口**实时**查询与修改场景；页面自己负责重绘。

注入方式：我们在页面加载完成后 `runJavaScript` 注入一段引导脚本，它把
`window.__narrativePrevisHost` 暴露出来。所有调用都是一个函数 + 一个 JSON 参数，
返回 Promise，结果必须是 JSON 可序列化的对象（渲染结果用 base64 PNG）。

## 接口

| 方法 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `hello()` | — | `{ok, appVersion, protocol: 1}` | 握手，主程序据此判断接口可用 |
| `getScene()` | — | `{sceneId, name, duration_ms, fps, aspect, entities[], cuts[]}` | 只读快照；`entities` 含 id/kind/name/position/rotation/scale/height/camera |
| `listCameras()` | — | `[{id, name, position, target, focal}]` | 机位清单（含 path 关键点时间） |
| `addEntity(spec)` | `{kind, name, asset?, position, rotation?, scale?, height?, camera?}` | `{id}` | 新增盒体/人物/机位；`kind` 只允许 `actor`/`prop`/`camera` |
| `updateEntity(id, patch)` | patch 为上述字段子集 | `{ok}` | 局部更新 |
| `removeEntity(id)` | — | `{ok}` | 删除 |
| `setCamera(id, spec)` | `{position, target, focal, framing?}` | `{ok}` | 改机位（含朝向与焦距） |
| `setCuts(cuts)` | `[{time_ms, cameraId}]` | `{ok}` | 设置时间→机位切换表 |
| `setTime(ms)` | — | `{ok}` | 把播放头移到指定时刻（抓帧与预览对齐用） |
| `captureFrame({width, height, cameraId, time_ms})` | — | `{png_base64}` | 按指定机位渲染一帧（离屏，不改播放状态） |
| `onChange(cb)` | 回调 | `{ok}` | 场景变更后回调一次（用于主程序刷新框图缓存） |
| `setReadability(options)` | `{persons?, roomOutlines?, kindColors?, labels?, all?}` | `{ok, applied}` | 可读性标注开关（只改显示，不写工程、不进撤销栈） |
| `applyBatch(ops)` | `[{op:"add"\|"update"\|"remove"\|"setCamera"\|"setCuts", …}]` | `{ok, results[]}` | 批量事务：全部成功或全部不生效（助手铺场景用这一条） |

## 约定

- **坐标系与单位**：与 DirectorDesk 一致——米、y 向上、`rotation` 弧度、`focal` 毫米。
- **id**：由页面生成并返回，主程序只保存。
- **失败**：返回 `{error: "可读原因"}`，不抛裸异常；批量里任一失败整批不生效并报出第几条。
- **不变式**：`captureFrame` 不改变播放状态；`applyBatch` 不产生中间态重绘。
- **版本**：`hello().protocol` 变化即视为不兼容，主程序应报“预演接口版本不匹配”。

## 主程序侧用法

- 抓框图：`setTime` → `captureFrame` 存 PNG（作为该帧的确认图），框图本身由
  `previs_scene.project` 从 `getScene()` 的快照算（离线、可测），两者用同一份机位参数。
- 助手铺场景：一次 `applyBatch`（新增墙/门/家具/机位），失败即整批回滚。
- 人工微调：在页面上直接拖；`onChange` 触发后主程序刷新本场快照与 revision。

## 实现时确认的细节（v1 实测补充）

| 项 | 事实 |
|---|---|
| 调用信封 | 参数一律是一个 JSON 对象：`applyBatch({ops:[…]})`、`setCuts({cuts:[…]})`、`updateEntity({id,patch})`、`setCamera({id,spec})`——页面两种写法都接受，宿主按这版发 |
| `hello()` 会阻塞 | 页面最长等 **20 秒**才回「未初始化完成」；宿主超时必须比它长（实现取 25 秒），否则只能看到笼统的超时 |
| `captureFrame` 尺寸 | 页面限制 **64..4096**，默认 1280×720 |
| `framing` 取值 | 页面是中文预设 `全景 / 中景 / 近景 / 特写`，`wide/medium/close` 由宿主映射 |
| `getScene` 还返回 | `revision`、`room:{enabled,width,depth,height}`、`path.points[].time_ms`、`clips`、`visible/locked` |
| 房间 | **没有写入口**：`room` 只能读与校验，`set_scene_layout(room=…)` 不符时整批拒绝，不假装成功 |
| 同批 id | `add` 在同一批里返回 id，但后续 op 只能引用**已知** id |
| `kind` | 页面还有 `crowd`，投影层不支持，快照里会丢弃 |
| `onChange` | 需要真 JS 函数，不能经 JSON 订阅；由 QML/桥槽位订阅后回调宿主 |
