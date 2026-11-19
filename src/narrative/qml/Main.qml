import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Fusion as Fusion
import QtQuick.Layouts
import QtQuick.Window

// Shell with three modes:
//   launcher  — project list (the entry point)
//   workspace — the open project: navigation, pages, status bar
//   settings  — 全局默认值（外观、harness 渠道、MCP、技能）与本工程的设置，
//               同一页里的两组，由导航最后那一项「设置」进出
// Pages own their state and subscribe to the bridge themselves.
ApplicationWindow {
    id: app
    objectName: "nativeRoot"
    width: 1440
    height: 900
    minimumWidth: theme.window_min_width
    minimumHeight: 680
    visible: true
    // 沉浸式：不要系统标题栏。窗口按钮长在页面自己的顶栏里，拖动交给页头空白处。
    flags: Qt.Window | Qt.FramelessWindowHint
    title: mode === "workspace" && project.title
        ? project.title + " · 叙事图工作台" : "叙事图工作台"
    color: app.theme.paper

    // 显式背景：窗口清屏色不进 scene graph，页面之间的空隙会露出未绘制的底，
    // 截图和离屏渲染都会变成黑块。
    background: Rectangle { color: app.theme.paper }

    // -- design tokens -------------------------------------------------
    // 令牌只有一份：native/theme.py 并列定义浅色与深色两套，bridge.theme 给出当前
    // 外观解析后的那一套（颜色是十六进制字符串，QML 直接当 color 用）。深浅切换时
    // 整张表被替换，所有绑定自动重算；页面只认令牌名，不认具体颜色。
    readonly property var theme: bridge.theme


    // 主框构：紧凑（默认，各区域共边）或岛屿（各区域浮成独立卡片，卡片间留缝）。
    // 岛屿只是加一层留白与圆角：所有 inset 在紧凑模式下都是 0，渲染与改动前一致。
    readonly property bool islandsMode: bridge.layoutMode === "islands"
    readonly property real islandInset: islandsMode ? theme.island_gap / 2 : 0
    readonly property real islandRadius: islandsMode ? theme.island_radius : 0

    // 懒创建组件的宿主别名：在 Component 里写 `app: app` 会解析成页面自己的 app
    // 属性（那是 undefined），必须通过只指向窗口的别名传进去。
    readonly property var windowRoot: app

    // -- 启动打点 -------------------------------------------------------
    // 只有 scripts/startup_bench.py 会传 --startup-trace；正常启动时这个布尔量是 false，
    // 下面每处打点的第一行就短路返回，不建对象、不读时钟、不发信号。
    // 打点写在**属性绑定**里而不是 Component.onCompleted：实测 onCompleted 在这一层
    // 全被推迟到整棵树建完之后才发，时间戳挤在最后几十毫秒里，量不出分段。
    // 属性绑定按文档顺序求值，所以「某个属性写在该元素最后」= 该子树建完的时刻。
    readonly property bool startupTrace: Qt.application.arguments.indexOf("--startup-trace") >= 0
    function startupMark(name) { console.log("STARTUP " + name + " " + Date.now()); return 0 }

    // -- shared state ---------------------------------------------------
    property var project: ({})
    property bool busy: false
    property string errorText: ""
    // 收到过哪些结果（自检用）：卡在空启动页时，这一行说明界面到底收到了什么。
    property var resultKinds: []
    // 墙钟也放进去：截图上的时间比实际旧，就说明窗口没在重绘（画面冻住了）。
    property string clock: Qt.formatTime(new Date(), "hh:mm:ss")
    property string diagnosticLine: "就绪 · " + app.mode
        + " · 工程 " + launcherPage.projectCount
        + " · 收到 " + resultKinds.length + " 条"
        + (busy ? " · 处理中 " + bridge.pendingKinds().join(",") : "")
        + " · " + clock

    // 每秒一跳。跳晚了就说明界面线程被占住了——那才是「点了很久才响应」的原因。
    property double lastTick: 0
    Timer {
        interval: 1000
        running: true
        repeat: true
        onTriggered: {
            var now = Date.now()
            if (app.lastTick > 0 && now - app.lastTick > 1500)
                console.log("LAG " + (now - app.lastTick) + "ms")
            app.lastTick = now
            app.clock = Qt.formatTime(new Date(), "hh:mm:ss")
        }
    }
    property string notice: ""
    property string mode: "launcher"
    // 设置页是从哪一页进来的：返回时原路回去，不从工作区把人甩回工程列表。
    // 单独一个字段而不是靠 mode 反推——进设置时 mode 已经被改成 "settings" 了。
    property string modeBeforeSettings: "launcher"
    property string currentPage: "browse"
    property int storyboardRequestSequence:0
    property var storyboardSelection:({})
    function nextStoryboardToken(){return ++storyboardRequestSequence}
    property string screenplayId: ""
    property var screenplaySelection: ({})
    // 常驻协作者：面板跨页面保留，收起后状态还在（消息不丢）。
    property bool assistantOpen: false
    // 宽窗口停靠成独立一列，窄窗口改成覆盖在页面区上的抽屉（不占列）。
    // 阈值只认令牌：见 theme.LAYOUT 的停靠态等式。
    readonly property bool assistantDocked: app.width >= theme.assistant_dock_min_width
    property bool navigationCollapsed:false
    property real navigationExpandedWidth:196
    function toggleNavigation(){if(!navigationCollapsed)navigationExpandedWidth=Math.max(150,sidebar.width);navigationCollapsed=!navigationCollapsed;sidebar.SplitView.preferredWidth=navigationCollapsed?56:navigationExpandedWidth}

    // 每项只有键名与页面名：这里曾经还有一句功能副标题（「逐镜制作与状态」之类），
    // 与页面名重复、又白占一行高度，已经去掉。导航行因此是单行的（高度见 delegate）。
    readonly property var navItems: [
        {key: "browse", label: "数据浏览"},
        {key: "relations", label: "关系图"},
        {key: "source", label: "原文"},
        {key: "adaptation", label: "改编"},
        {key: "assets", label: "资产"},
        {key: "screenplay", label: "剧本"},
        {key: "storyboard", label: "分镜稿"},
        // 镜头表夹在分镜稿与预演之间：先按场排镜（分镜稿），再逐镜看状态（镜头表），
        // 再看三维预演摆成什么样，最后进导演台调调度。图标借用分镜稿那一档——
        // NavigationIcon 里还没有它自己的画法，而逐镜看的就是同一批分镜格。
        {key: "shot_table", label: "镜头表", icon: "storyboard"},
        // 预演进导演台之前（先看效果，再定调度）。
        {key: "previs", label: "预演"},
        {key: "director", label: "导演台"},
        {key: "screenplay_structure", label: "结构"},
        {key: "static_check", label: "静态检查"},
        // 设置是导航里的最后一项（原来「工程设置」的位置）：工程内只留这一个入口，
        // 进去以后才分「全局」与「本工程」两组。它不是工作区的一页，所以没有对应的
        // Loader 位置：点它走 openSettings 换 mode，不切 currentPage。
        // 图标借 NavigationIcon 里现成的齿轮画法（project_settings 那一档）。
        {key: "settings", label: "设置", icon: "project_settings"}
    ]

    function goTo(key) {
        currentPage = key
    }

    // 导航里哪一项算「当前」：设置打开时高亮「设置」这一项，其余时候按页面算。
    // 单独一列而不是就地比较 currentPage——设置不是页面位置（currentPage 不变），
    // 直接比较会让「设置」永远不高亮，点开了也看不出自己在哪。
    readonly property string navSelection: app.mode === "settings" ? "settings" : app.currentPage

    // -- 页面按需创建 ---------------------------------------------------
    // 打开工程只建当前这一页，其余等第一次进入再实例化：原文、分镜这类页面
    // 自己的初始化代价不小，一开始全建一遍就是「打开工程要等」。
    property var visitedPages: ({})
    function pageVisited(key) { return visitedPages[key] === true }
    function markPageVisited(key) {
        if (visitedPages[key] === true) return
        var copy = {}
        for (var name in visitedPages) copy[name] = visitedPages[name]
        copy[key] = true
        visitedPages = copy
    }
    // 页面文件在这里才被读进来（连编译一起推迟）：初始属性在页面的
    // Component.onCompleted 之前落下，所以页面「建好就读数据」那套照旧成立。
    function loadPage(loader, file) {
        if (!loader || loader.item) return
        loader.setSource(Qt.resolvedUrl(file), { "app": windowRoot })
    }

    // -- 预演页预热 ------------------------------------------------------
    // 工程打开、界面空闲 300 ms 后「假装访问过一次」预演页：Loader 照常实例化
    // DirectorWorkspacePage，它自己起本地服务、建 WebEngineView 并开始加载——但
    // StackLayout 只显示当前页，所以这一页在屏幕外加载，用户看不到任何闪动。
    // 为什么值得：离屏基准（scripts/previs_load_bench.py）里第一次打开要 ~3.0 秒才等到
    // 页面接口可用（~0.8 秒是 WebEngine 起渲染进程，其余是入口 chunk 的链式加载），
    // 而同一个渲染进程里再开一次只要 ~1.0 秒。把这段时间挪到用户看别的页面时，
    // 点「预演」就只剩切换。
    // 为什么用 markPageVisited()：它就是上面这些 Loader 用的那个开关，所以接线只需要
    // 这一处，不必改 Loader 的 active 绑定。pageVisited 只被这些 Loader 读，
    // 提前置位等于「已备好」。
    // 为什么看 GraphicsInfo：软件渲染（离屏、截图脚本、测试）下 WebGL 起不来，页面必然
    // 报「DirectorDesk startup failed」，预热只是白烧 3 秒 CPU——那种情况不预热。
    Timer {
        objectName: "previsPreheat"
        interval: 300
        repeat: false
        running: app.mode === "workspace" && !app.pageVisited("previs")
                 && GraphicsInfo.api !== GraphicsInfo.Unknown
                 && GraphicsInfo.api !== GraphicsInfo.Software
        onTriggered: app.markPageVisited("previs")
    }

    onCurrentPageChanged: markPageVisited(currentPage)
    Component.onCompleted: {
        // 根对象最后完成：这一行 ≈ 整次 load 的收尾时刻。
        if (startupTrace) startupMark("root")
        // 这里曾经也 markPageVisited(currentPage)：启动时 currentPage 的默认值是
        // browse，于是「还没进工程」就先把 136 KB 的浏览页建了出来。现在只在真的
        // 进工作区（enterWorkspace）或翻到某一页时才标记，启动一个页面都不建。
    }

    // 从节点/属性跳回原文：切到原文页并把引文位置滚到眼前。
    function openSource(sourceId, start) {
        app.openSourceSpan(sourceId, start, -1)
    }

    function openSourceSpan(sourceId, start, end) {
        if (!sourceId) return
        currentPage = "source"
        // 页面是懒创建的：等它建好再交给它定位。
        Qt.callLater(function () {
            if (sourceLoader.item) sourceLoader.item.load(sourceId, start, end)
        })
    }

    function focusBrowseNode(nodeId, target) {
        currentPage = "browse"
        Qt.callLater(function () {
            if (browseLoader.item) browseLoader.item.focusIssue(nodeId, target || ({}))
        })
    }

    function toast(message, isError) {
        if (isError) errorText = message
        else notice = message
    }

    function enterWorkspace() {
        mode = "workspace"
        currentPage = "browse"
        markPageVisited(currentPage)
    }

    // 工程内的唯一设置入口是导航列表里最后那一项「设置」，启动页上另有一个；
    // 两处都走这里：来源页记在 modeBeforeSettings 里，设置页据此决定默认分组
    // （本工程 / 外观），closeSettings 也据此知道该回哪一页。
    function openSettings() {
        if (mode !== "settings") modeBeforeSettings = mode
        mode = "settings"
    }

    function closeSettings() {
        if (mode !== "settings") return
        mode = modeBeforeSettings === "workspace" ? "workspace" : "launcher"
    }

    // 设置页只在第一次进设置时创建，之后一直留着：离开再回来不重建，
    // 分组停留位置与表单不闪一下。改 mode 的地方不止 openSettings（返回、关工程、
    // 测试里直接摆属性），所以按 mode 的变化来记，而不是只在入口函数里记。
    property bool settingsVisited: false
    // 进过一次工作区就在这儿留个记号：导航栏那一列第一次进去时才建，之后留着
    // （折叠状态、滚动位置不丢）。按 mode 的变化记，直接摆 mode 的调用方也照顾到。
    property bool workspaceVisited: false
    // 协作者面板同理：它收起着就不建，第一次打开时才建（60 KB 的 QML，实测 200 ms）。
    property bool assistantVisited: false
    onAssistantOpenChanged: if (assistantOpen) assistantVisited = true
    onModeChanged: {
        if (mode === "workspace") workspaceVisited = true
        if (mode !== "settings") return
        // 这一趟之前就建好了吗？刚被创建出来的那个页面在 Component.onCompleted
        // 里已经进过一遍了，别重复读磁盘。
        var existing = settingsLoader.item
        settingsVisited = true
        if (existing) existing.enter()
    }

    function backToLauncher() {
        bridge.closeProject()
        project = ({})
        mode = "launcher"
    }

    Connections {
        target: bridge
        function onResultReady(kind, payload) {
            resultKinds = resultKinds.concat([kind])
            if (kind === "project") {
                screenplayId = ""
                screenplaySelection = ({});storyboardSelection=({})
                errorText = ""
                project = payload.project || ({})
                notice = "已打开 " + (project.title || project.name || "工程")
                enterWorkspace()
            } else if (kind === "project_renamed") {
                project = payload.project || project
                notice = "工程名称已更新为 " + (project.title || "")
                bridge.loadRecentProjects()
            } else if (kind === "project_closed") {
                screenplaySelection = ({});storyboardSelection=({})
                screenplayId = ""
                mode = "launcher"
                project = ({})
                assistantOpen = false
            } else if (kind === "static_check_target" && !payload.superseded) {
                if (payload.kind === "source") app.openSourceSpan(payload.source_id, payload.start, payload.end)
                else app.focusBrowseNode(payload.node_id, payload)
            } else if (kind === "conversation_progress") {
                if (payload.graph_reset_required && payload.revision !== undefined
                    && (!payload.project_path || bridge.recentProjectKey(payload.project_path) === bridge.recentProjectKey(project.path || ""))) {
                    var refreshed = ({})
                    for (var field in project) refreshed[field] = project[field]
                    refreshed.revision = payload.revision
                    project = refreshed
                }
                if (mode === "workspace" && payload.event === "task_started"
                    && (!payload.project_path || bridge.recentProjectKey(payload.project_path) === bridge.recentProjectKey(project.path || "")))
                    assistantOpen = true
            } else if (kind === "node_edit" || ((kind === "assistant" || kind === "rollback") && payload.graph_reset_required)) {
                if (payload.revision !== undefined) {
                    var updated = ({})
                    for (var key in project) updated[key] = project[key]
                    updated.revision = payload.revision
                    project = updated
                }
                notice = "修订号 " + (payload.revision !== undefined ? payload.revision : project.revision)
            }
        }
        function onErrorOccurred(kind, message) {
            errorText = message
            // A failed open leaves the launcher in place; never a half-entered project.
            if (kind === "project" && mode !== "workspace") project = ({})
        }
        // 非致命提示：动作已经照用户的意思做完了，只是附带记录（如最近工程列表）没写进去。
        // 用提示色而不是错误色——染成红色会让人以为操作失败了。
        function onNoticeRaised(message) {
            notice = message
        }
        function onBusyChanged(value) {
            busy = value
        }
    }

    // 启动 6 秒后自检一次：一次运行只写一行。启动页停在「处理中」时，
    // 这行直接说明界面自己以为在等什么、收到过什么。
    Timer {
        interval: 6000
        running: true
        repeat: false
        onTriggered: console.log("SELFTEST busy=" + app.busy + " mode=" + app.mode
            + " projects=" + launcherPage.projectCount
            + " kinds=" + app.resultKinds.join("/")
            + " pending=" + bridge.pendingKinds().join(",")
            + " error=" + (app.errorText || "-"))
    }

    // 顶部拖动带：只接住"没人要"的点击——按钮、输入框自己会吃掉鼠标事件，
    // 空白处的按下就落到这一层，于是整条顶部（不是 8px 窄条）都能拖窗口。
    // 双击切换最大化；拖到屏幕顶部由系统贴边最大化（Aero Snap）。
    DragHandle {
        objectName: "dragBand"
        app: app
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 46
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            // 下标按声明顺序：0 启动页 / 1 工作区 / 2 设置页。设置页排在最后，
            // 启动页与工作区的下标保持原样（原有的懒创建与几何断言都依赖它们）。
            currentIndex: app.mode === "launcher" ? 0 : app.mode === "workspace" ? 1 : 2

            LauncherPage { id: launcherPage; app: app; property int __trace: app.startupTrace ? app.startupMark("launcherPage 建完") : 0 }


            // -- workspace ----------------------------------------------
            // 导航栏和页面之间也能拖：宽一点看长节点名，窄一点留给正文。
            SplitView {
                id: workspaceColumns
                objectName: "workspaceColumns"
                orientation: Qt.Horizontal
                property int __traceBegin: app.startupTrace ? app.startupMark("workspace 开始") : 0

                handle: Rectangle {
                    objectName: "workspaceColumnHandle"
                    implicitWidth: 6
                    color: "transparent"

                    // 只负责把光标变成左右箭头；按键留给 SplitView 自己处理。
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.SplitHCursor
                        acceptedButtons: Qt.NoButton
                    }

                    Rectangle {
                        anchors.centerIn: parent
                        width: 2
                        height: parent.height
                        visible: !app.islandsMode
                        color: SplitHandle.pressed || SplitHandle.hovered
                            ? app.theme.accent : app.theme.line
                    }
                }

                Rectangle {
                    id:sidebar;objectName:"applicationNavigation"
                    SplitView.preferredWidth:app.navigationCollapsed?56:app.navigationExpandedWidth
                    SplitView.minimumWidth:app.navigationCollapsed?56:150
                    SplitView.maximumWidth:app.navigationCollapsed?56:320
                    SplitView.fillHeight: true
                    // 卡片画在内层矩形上：岛屿模式它比外框小一圈，紧凑模式下贴边即与原样一致。
                    color: "transparent"
                    border.width: 0

                    Rectangle {
                        id: sidebarCard
                        anchors.fill: parent
                        anchors.margins: app.islandInset
                        color: app.theme.card
                        border.color: app.theme.line
                        radius: app.islandRadius
                    }

                    // 导航栏的内容按需创建：启动页/设置页里这一列一个字都看不见，但它
                    // 有十来件控件（返回、收起、标题、修订、路径、状态行、版本、警告、
                    // 滚动区 + 13 个导航项），实测整块建出来要 1 s 上下——比启动时其余
                    // 所有东西加起来还多（逐个控件拿掉都只在噪声里，所以只能整块推迟）。
                    // 第一次进工作区才建，之后留着：导航折叠状态与滚动位置都不丢。
                    Loader {
                        id: navigationLoader
                        anchors.fill: sidebarCard
                        active: app.workspaceVisited || app.mode === "workspace"
                        sourceComponent: navigationColumnComponent
                    }
                    Component {
                        id: navigationColumnComponent
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins:app.navigationCollapsed?6:14
                        spacing: 4

                        Button {
                            objectName: "backToProjectsButton"
                            Layout.fillWidth: true
                            flat: true
                            text:app.navigationCollapsed?"":"← 工程列表"
                            implicitWidth:app.navigationCollapsed?40:150
                            NavigationIcon{visible:app.navigationCollapsed;name:"back";ink:app.theme.ink;anchors.centerIn:parent;width:24;height:24}
                            ToolTip.visible:hovered
                            ToolTip.text:"工程列表"
                            onClicked: app.backToLauncher()
                            property int __trace: app.startupTrace ? app.startupMark("① 返回按钮建完") : 0
                        }

                        ToolButton{objectName:"navigationCollapse";text:app.navigationCollapsed?">":"< 收起导航";Layout.fillWidth:true;implicitWidth:40;onClicked:app.toggleNavigation();ToolTip.visible:hovered;ToolTip.text:app.navigationCollapsed?"展开导航":"收起导航";property int __trace: app.startupTrace ? app.startupMark("② 收起按钮建完") : 0}
                        Label {
                            visible:!app.navigationCollapsed
                            Layout.fillWidth: true
                            Layout.topMargin: 8
                            text: project.title || project.name || "未命名工程"
                            color: app.theme.ink
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            visible:!app.navigationCollapsed
                            text: "修订 " + (project.revision !== undefined ? project.revision : "—")
                            color: app.theme.muted
                            font.pixelSize: 11
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            Layout.topMargin: 8
                            Layout.bottomMargin: 8
                            color: app.theme.line
                        }

                        ScrollView {id:navScroll;objectName:"navigationScroll";contentWidth:availableWidth;Layout.fillWidth:true;Layout.fillHeight:true;clip:true
                            ColumnLayout {width:navScroll.availableWidth;spacing:4
                        Repeater {
                            model: app.navItems
                            delegate: Item {
                                required property var modelData
                                objectName: "navigation_" + modelData.key
                                Layout.fillWidth: true
                                // 单行项：副标题去掉之后 46 会留出半行空白。
                                Layout.preferredHeight: 34

                                Rectangle {
                                    anchors.fill: parent
                                    radius: 6
                                    color: app.navSelection === modelData.key
                                        ? app.theme.accentSoft : "transparent"
                                }
                                // 图标默认按导航键名画；个别页面没有自己的画法时用 icon 指定借哪一档。
                                NavigationIcon {name:modelData.icon||modelData.key;ink:app.navSelection===modelData.key?app.theme.accent:app.theme.ink;width:24;height:24;anchors.verticalCenter:parent.verticalCenter;x:app.navigationCollapsed?(parent.width-width)/2:10}
                                Label {
                                    visible:!app.navigationCollapsed
                                    anchors.left: parent.left
                                    anchors.leftMargin: 44
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.label
                                    color: app.navSelection === modelData.key
                                        ? app.theme.accent : app.theme.ink
                                    font.pixelSize: 14
                                    font.weight: app.navSelection === modelData.key
                                        ? Font.DemiBold : Font.Normal
                                }
                                ToolTip.visible:navMouse.containsMouse
                                ToolTip.text:modelData.label
                                MouseArea {
                                    id:navMouse;hoverEnabled:true
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    // 设置不是一个页面位置，而是换 mode 打开设置页；
                                    // 其余项才是「切到这一页」。
                                    onClicked: modelData.key === "settings"
                                        ? app.openSettings() : app.goTo(modelData.key)
                                }
                            }
                        }

                            }
                        }
                        property int __trace: app.startupTrace ? app.startupMark("③ 导航滚动区建完") : 0

                        // 设置入口不在这里：它是导航列表的最后一项（navItems 的
                        // settings），和「这个工程有哪些页面」并排，只是点它进的是设置页。
                        // 这一块剩下的是「这台机器 / 这个工程的身份」：路径与版本。
                        Label {
                            Layout.fillWidth: true
                            visible:!app.navigationCollapsed
                            text: project.path || ""
                            color: app.theme.muted
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
                        }

                        // 原来底部信息栏的那一行自检与版本号搬到这里：页面不占那一行，
                        // 但「卡在等待」「低完整性」「构建身份」这些现场记录仍然看得见。
                        Label {
                            objectName: "statusLine"
                            Layout.fillWidth: true
                            visible: !app.navigationCollapsed
                            elide: Text.ElideRight
                            font.pixelSize: 10
                            text: app.errorText.length > 0 ? app.errorText
                                : app.notice.length > 0 ? app.notice
                                : app.diagnosticLine
                            color: app.errorText.length > 0 ? app.theme.bad : app.theme.muted
                        }
                        Label {
                            objectName: "appVersionLabel"
                            Layout.fillWidth: true
                            visible: !app.navigationCollapsed
                            text: bridge.appVersion
                            color: app.theme.muted
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
                        }
                        Label {
                            objectName: "integrityWarningLabel"
                            Layout.fillWidth: true
                            visible: bridge.integrityWarning.length > 0 && !app.navigationCollapsed
                            text: bridge.integrityWarning
                            color: app.theme.warn
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                        property int __traceEnd: app.startupTrace ? app.startupMark("导航列建完（Repeater 的 13 项还没建）") : 0
                    }
                    }
                }

                // 页面区：窄窗口下协作者要覆盖在它上面，所以外面包一层 Item——
                // 抽屉和页面列共用同一块几何，SplitView 只认这一层的最小宽度。
                Item {
                    id: workspacePageArea
                    objectName: "workspacePageArea"
                    SplitView.fillWidth: true
                    SplitView.minimumWidth: app.theme.workspace_min_width
                    Layout.fillHeight: true
                    clip: true

                ColumnLayout {
                    anchors.fill: parent
                    // 页面这一列吃掉剩下的宽度：协作者展开时只让出它自己的 360。
                    spacing: 0

                    // 页面内容区只是容器，自己不画卡片：岛屿模式下让窗口底色 paper 从
                    // 各页面自己的列/卡片之间露出来——页面里的 card 才是岛屿，缝隙才有
                    // 层次。紧凑模式的边距本来就是 0、颜色本来就是透明，渲染与改动前一致。
                    Rectangle {
                        id: pageCard
                        objectName: "pageCard"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.leftMargin: app.islandInset
                        Layout.rightMargin: app.islandInset
                        Layout.bottomMargin: app.islandInset
                        color: "transparent"
                        border.width: 0

                        StackLayout {
                        anchors.fill: parent
                        SplitView.fillWidth: true
                        SplitView.minimumWidth: 520
                        SplitView.fillHeight: true
                        // 页面位置 = navItems 里这一页的下标。navItems 末尾的「设置」
                        // 没有对应的 Loader：它换的是 mode，currentPage 从不等于
                        // "settings"，所以这里永远只会落到前面那些页的位置上。
                        currentIndex: {
                            for (var i = 0; i < app.navItems.length; ++i)
                                if (app.navItems[i].key === app.currentPage) return i
                            return 0
                        }

                        // 每个位置一个 Loader：第一次进入那一页才把页面文件读起来，之后保留。
                        // 为什么用 setSource(url) 而不是 sourceComponent：只要 Main.qml 里
                        // 写着 `Component { BrowsePage {} }`，那个页面文件在 load 时就会被
                        // 编译——哪怕它的 Loader 是 active:false。12 个页面文件加起来是启动
                        // 时间里最大的一笔（二分实测 1.3 s）。setSource 连编译一起推迟到
                        // 真正进入那一页时；初始属性在页面的 Component.onCompleted 之前
                        // 落下，所以页面自己那套「建好就读数据」的初始化照旧能读到 app。
                        Loader { id: browseLoader; active: app.pageVisited("browse"); onActiveChanged: if (active) app.loadPage(browseLoader, "BrowsePage.qml") }
                        Loader { id: relationsLoader; active: app.pageVisited("relations"); onActiveChanged: if (active) app.loadPage(relationsLoader, "RelationPage.qml") }
                        Loader { id: sourceLoader; active: app.pageVisited("source"); onActiveChanged: if (active) app.loadPage(sourceLoader, "SourcePage.qml") }
                        Loader { id: adaptationLoader; active: app.pageVisited("adaptation"); onActiveChanged: if (active) app.loadPage(adaptationLoader, "AdaptationPage.qml") }
                        Loader { id: assetsLoader; active: app.pageVisited("assets"); onActiveChanged: if (active) app.loadPage(assetsLoader, "AssetsPage.qml") }
                        Loader { id: screenplayLoader; active: app.pageVisited("screenplay"); onActiveChanged: if (active) app.loadPage(screenplayLoader, "ScreenplayPage.qml") }
                        Loader { id: storyboardLoader; active: app.pageVisited("storyboard"); onActiveChanged: if (active) app.loadPage(storyboardLoader, "StoryboardPage.qml") }
                        Loader { id: shotTableLoader; active: app.pageVisited("shot_table"); onActiveChanged: if (active) app.loadPage(shotTableLoader, "ShotTablePage.qml") }
                        // 预演页的 QML 里 import 了 QtWebEngineQuick：它只在这一页被真正
                        // 打开时才编译，没有 WebEngine 的环境下别的页面完全不受影响。
                        Loader { id: previsLoader; active: app.pageVisited("previs"); onActiveChanged: if (active) app.loadPage(previsLoader, "DirectorWorkspacePage.qml") }
                        Loader { id: directorLoader; active: app.pageVisited("director"); onActiveChanged: if (active) app.loadPage(directorLoader, "DirectorDeskPage.qml") }
                        Loader { id: structureLoader; active: app.pageVisited("screenplay_structure"); onActiveChanged: if (active) app.loadPage(structureLoader, "ScreenplayStructurePage.qml") }
                        Loader { id: staticCheckLoader; active: app.pageVisited("static_check"); onActiveChanged: if (active) app.loadPage(staticCheckLoader, "StaticCheckPage.qml") }
                    }
                    }
                }

                // 窄窗口的协作者抽屉：不占 SplitView 列，覆盖在页面内容卡之上。
                // 顶栏（标题、路径、「协作者」按钮）不盖：遮罩压上去按钮就点不到了。
                // 遮罩用令牌色加透明度，不新增颜色；点遮罩或再点「协作者」收起。
                Item {
                    id: assistantOverlay
                    objectName: "assistantOverlay"
                    anchors.fill: parent
                    // pageCard 与这里不是兄弟，只能借它的 y 当上边距；这个值已经含
                    // 顶栏高度与岛屿内缩，绑定会跟着布局变，不写死 54。
                    anchors.topMargin: pageCard.y
                    visible: app.assistantOpen && !app.assistantDocked
                    z: 100

                    Rectangle {
                        objectName: "assistantScrim"
                        anchors.fill: parent
                        color: app.theme.ink
                        opacity: 0.18

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: app.assistantOpen = false
                        }
                    }
                    // 面板本身在下面的停靠位里声明：只有一份实例，按模式换 parent。
                }
                }

                // 宽窗口：协作者停靠成独立一列，切页面不消失。
                // 岛屿模式下它比面板多留一圈缝，面板自己就成了一张独立卡片。
                Item {
                    id: assistantDockSlot
                    objectName: "assistantDock"
                    visible: app.assistantOpen && app.assistantDocked
                    SplitView.preferredWidth: 360 + (app.islandsMode ? app.theme.island_gap : 0)
                    SplitView.minimumWidth: 280
                    SplitView.fillHeight: true

                    // 一份面板、两个位置：停靠时是 SplitView 第三列，窄窗口时 parent
                    // 换到页面区上的抽屉里。同一实例，宽度跨过阈值时对话内容不丢。
                    // 面板收起时不建：它那 60 KB 的 QML 建一遍要 200 ms 上下（实测），
                    // 而启动时它一定是收起的。第一次打开建出来，之后开合只是显隐。
                    Loader {
                        id: assistantLoader
                        anchors.fill: parent
                        active: app.assistantVisited
                        sourceComponent: assistantPanelComponent
                    }
                    Component {
                        id: assistantPanelComponent
                        AssistantPanel {
                            id: assistantPanel
                            // 在 Component 里必须用只指向窗口的别名：写 `app: app`
                            // 会解析成面板自己的 app 属性（那时还是 undefined），
                            // 连带 parent/pageId 一起失效。
                            parent: windowRoot.assistantDocked ? assistantDockSlot : assistantOverlay
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            anchors.right: parent.right
                            anchors.margins: windowRoot.islandInset
                            width: windowRoot.assistantDocked
                                ? parent.width - 2 * windowRoot.islandInset
                                : Math.min(360 + (windowRoot.islandsMode ? windowRoot.theme.island_gap : 0),
                                           parent.width - 2 * windowRoot.islandInset)
                            app: windowRoot
                            pageId: windowRoot.currentPage
                            visible: windowRoot.assistantOpen
                            onCloseRequested: windowRoot.assistantOpen = false
                        }
                    }
                    property int __traceEnd: app.startupTrace ? app.startupMark("工作区收尾（Repeater 之前）") : 0
                }
            }

            // -- settings -----------------------------------------------
            // 设置单独成一页，里面再分「全局」（外观 / 模型渠道 / MCP / 技能）与
            // 「本工程」（工程设置 / 关于）两组。从启动页或工作区进来，返回时由
            // closeSettings 回到进来的那一页（工程仍开着就从工作区回去，不会丢现场）。
            // 这一页曾经常驻在 StackLayout 里：不进设置也会被编译并建出来（含技能面板
            // 47 KB、MCP 面板 28 KB、内置提示词列表）。现在第一次进设置才建，之后留着。
            Loader {
                id: settingsLoader
                active: app.settingsVisited
                onActiveChanged: if (active) app.loadPage(settingsLoader, "SettingsPage.qml")
            }
            property int __traceEnd: app.startupTrace ? app.startupMark("外壳建完（StackLayout 收尾）") : 0
        }

    }


    // 启动页与设置页的窗口按钮与拖动区：浮在内容之上，不占布局（放在 StackLayout 里会被当成新的一页）。
    // 设置页也一起接住：窗口无边框，少了这一层，在设置里既不能拖也不能关窗口。
    Item {
        objectName: "launcherWindowChrome"
        visible: app.mode !== "workspace"
        anchors.fill: parent
        z: 500

        DragHandle {
            app: app
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.right: parent.right
            height: 44
        }
        WindowControls {
            app: app
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.topMargin: 10
            anchors.rightMargin: 12
        }
        property int __traceEnd: app.startupTrace ? app.startupMark("窗口浮动件建完") : 0
    }
}
