# 导演台：整页 Web 工作区（P0 验证结论）

新导演台不再在 QML 里重建 3D，而是**整页承载 DirectorDesk 的构建产物**（MIT），QML 只留导航与"返回分镜稿"。这份文档记的是验证数据与接入约定。

## 一、验证结论（2026-11-18，本机）

构建它的 `vite` 产物（6.2 MB）放在 QtWebEngine 里直接打开：

| 项 | 实测 |
|---|---|
| 起得来吗 | **能**：完整界面（文件/编辑/视图菜单、布景/并排/拍摄三态、时间轴、检查器）渲染正常 |
| WebGL | 可用（`webgl: true`），three.js 正常出图 |
| 帧率 | **60 fps**（3 秒 181 帧，1600×1000 窗口） |
| 视口拖拽 | 20 步模拟拖动耗时 487 ms（约 24 ms/步，含脚本里的 8 ms 等待） |
| 桌面桥缺失时 | **不报错、优雅降级**（`hasDesktopBridge: false`，界面照常可用） |

截图与实测原始数据留在 `.runtime/spike/`（不入库）。

## 二、怎么跑起来

`file://` 打开会因 ES module / CORS 失败，必须走本地 HTTP：

- 启动时在 127.0.0.1 上开一个**随机端口**的静态服务（只读、只服务 `dist/`）；
- `QWebEngineView` 加载 `http://127.0.0.1:<port>/index.html`；
- 退出时关服务。

## 三、不必伪造桌面桥：用浏览器模式 + 接住下载

两次实验把这件事定死了：

| 做法 | 结果 |
|---|---|
| 注入 `window.directorDesktop`，只实现 `files` | **启动失败**（「无法启动导演台」）——它一旦检测到桌面桥就按桌面模式跑，桥面不全直接崩，不会退化 |
| **完全不注入**（浏览器模式） | 界面完整可用；点「保存」发出下载 `光影舞台.director`，**QtWebEngine 的 `downloadRequested` 接住并落盘成功**（9.9 KB） |

所以接入方式是：

```
QWebEngineProfile.downloadRequested
  → setDownloadDirectory(工程目录/previs/<unit_id>/)
  → accept()
```

保存、导出 PNG/MP4、导出分镜稿 HTML 都走这条下载通道，**不需要实现那 20 多个桌面 API**。

要隐藏或剔除的仍是那些依赖桌面能力的功能：

| 能力 | 处理 |
|---|---|
| 内置 AI 助手 / MCP / 技能 | 隐藏入口（我们的协作者承担） |
| 自动更新 | 隐藏 |
| Blender 工程导入 | 第一版隐藏 |
| 视频反推动作 | 构建时剔除（依赖非商业模型） |
| 插件 | 隐藏 |

## 三·五、产物从哪来、怎么构建

界面里的「预演」页按顺序找产物：

```
NARRATIVE_PREVIS_DIST        环境变量，指向构建输出（开发与打包都可）
<工程目录>/previs-web/        随工程分发
.runtime/vendor/director-desk/dist   开发期构建输出（当前用它）
```

构建（Windows，本项目实测可用的步骤）：

```bash
git clone --depth 1 https://github.com/mangfufu/director-desk.git .runtime/vendor/director-desk
cd .runtime/vendor/director-desk
pnpm install --no-frozen-lockfile     # 首次会提示忽略构建脚本
pnpm rebuild esbuild                  # esbuild 的二进制必须装，否则 vite 起不来
pnpm exec vite build                  # 产出 dist/（约 6.2 MB）
```

注意两点：`node` 要在 PATH 上（pnpm exec 会调用它）；产物的 QML 承载模块在 PySide6 6.12 里叫
**`QtWebEngine`**（不是 `QtWebEngineQuick`），且 `QtWebEngineQuick.initialize()` 必须在
`QGuiApplication` 之前调用。

## 三·六、配色：对齐到本项目令牌

它的界面是中性灰（`#131313` / `#e9e9e9`），与本项目的蓝灰深色令牌差得很明显。做法是
**只换配色、不改结构**：`previs_theme.py` 生成一段覆盖样式，把它页面里的底色、面板、线、
文字、按钮、输入框、滚动条、时间轴片段映射到 `theme.tokens(True)`，加载完成与主题切换时注入。

做法是**读它的样式表、把每个颜色重映射到本项目对应的主题阶梯，再整份注入**（十六进制与 `rgb()/rgba()` 都换）：
连写死的面板色、边框色、悬停色都一起换掉，不会漏掉某一块仍是灰的。饱和度高的颜色
（红/绿/黄这类语义色）保持原样。

浅色皮肤必须**反过来映射**：它的界面本来就是深色，若按原亮度映射，背景会依然深、界面看着还是深色。
深色皮肤按原亮度落点，浅色皮肤用 `1 - 亮度` 落到浅色阶梯。

两条边界：

- **3D 视口里的画面不染色**——颜色来自 canvas 与纹理，样式表管不到，天然安全；
- 结构、密度、字体归它自己：硬改这些会与上游越走越远。

## 三·六·五、预演页工具条

它自己的顶栏折成两行、样式也与我们不同，所以整条藏掉，由 `DirectorWorkspacePage.qml` 提供工具条：

| 控件 | 怎么驱动它 |
|---|---|
| 场景下拉 | 读它 DOM 里 `#scene-switch` 的选项；切换时改 `value` 并派发 `change` |
| 保存 / 导出 | `document.querySelector('[data-act="save"|"export"]').click()`（隐藏的按钮照样可点） |
| 窗口按钮 | 与工具条同一行，右侧常驻 |

这一小段接口由 `previs_theme.inject_script` 注入为 `window.__narrativePrevis`。

## 三·七、页面占用

预演页是整页工作区，所以：

| 元素 | 处理 |
|---|---|
| 我们的页面标题栏 | **所有页面都不要了**：各页自己画标题，内容从顶部开始 |
| 窗口按钮（最小化/最大化/关闭） | 不占行：浮在右上角，**鼠标移进右上角才出现**；顶部 8 px 透明条负责拖动、双击最大化 |
| 我们的底部状态栏 | 预演页隐藏 |
| 它自己的整条顶栏 | **压成 0 高并隐藏**，改由我们自己的工具条驱动（场景切换、保存、导出）。不能 `display:none`：它是 grid 子项，删掉会让后面的行整体上移一行 |
| 系统标题栏 | **全局去掉**：窗口无边框，最小化/最大化/关闭直接长在各页顶栏里（启动页浮在右上角），页头空白处可拖动 |

## 四、数据契约

- 场景（对象、走位、机位、时间轴）以**它的工程文件**为准，放在 `previs/<unit_id>/project.json`；
- 我们的 SQLite 只存：`unit_id`、`previs_revision`、机位快照（供分镜稿与素描使用）；
- 两边交换的只有「场次编号 + 修订 + 机位快照 + 渲染结果」，不做双向同步。

## 五、下一步（P1）

1. `DirectorWorkspace.qml`：WebEngineView + 本地静态服务 + 加载/错误/降级态 + "返回分镜稿"；
2. 桥：`files` 那 8 个 action，路径限制在工程目录内；
3. 构建接入：`vendor/director-desk` 固定版本 + 构建产物入库或打包时生成；
4. 隐藏清单落地：菜单项与面板入口逐项关掉，构建时剔除反推模块；
5. 助手：`previs_read` / `previs_apply_changes` / `previs_capture` 三个工具，让它能"看图改位"。
