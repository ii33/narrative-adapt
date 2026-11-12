import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Fusion as Fusion
import QtQuick.Layouts
import QtQuick.Window

// Shell with two modes:
//   launcher  — project list + global harness settings (the entry point)
//   workspace — the open project: navigation, pages, status bar
// Pages own their state and subscribe to the bridge themselves.
ApplicationWindow {
    id: app
    objectName: "nativeRoot"
    width: 1440
    height: 900
    minimumWidth: theme.window_min_width
    minimumHeight: 680
    visible: true
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

    readonly property var navItems: [
        {key: "browse", label: "数据浏览", hint: "按分类看节点"},
        {key: "relations", label: "关系图", hint: "人物与关系"},
        {key: "source", label: "原文", hint: "导入与回查"},
        {key: "adaptation", label: "改编", hint: "独立编辑叙事"},
        {key: "assets", label: "资产", hint: "形象、物品与视觉设定"},
        {key: "screenplay", label: "剧本", hint: "选段、场表与正文"},
        {key: "storyboard", label: "分镜稿", hint: "分镜文字、图格与导出"},
        {key: "director", label: "导演台", hint: "调度、机位与动态预演"},
        {key: "screenplay_structure", label: "结构", hint: "集、幕与段落"},
        {key: "static_check", label: "静态检查", hint: "约束与问题定位"},
        {key: "project_settings", label: "工程设置", hint: "提取粒度与模型"}
    ]

    function goTo(key) {
        currentPage = key
    }

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
    onCurrentPageChanged: markPageVisited(currentPage)
    Component.onCompleted: markPageVisited(currentPage)

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

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: app.mode === "launcher" ? 0 : 1

            LauncherPage { id: launcherPage; app: app }

            // -- workspace ----------------------------------------------
            // 导航栏和页面之间也能拖：宽一点看长节点名，窄一点留给正文。
            SplitView {
                id: workspaceColumns
                objectName: "workspaceColumns"
                orientation: Qt.Horizontal

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

                    ColumnLayout {
                        anchors.fill: sidebarCard
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
                        }

                        ToolButton{objectName:"navigationCollapse";text:app.navigationCollapsed?">":"< 收起导航";Layout.fillWidth:true;implicitWidth:40;onClicked:app.toggleNavigation();ToolTip.visible:hovered;ToolTip.text:app.navigationCollapsed?"展开导航":"收起导航"}
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

                        ScrollView {id:navScroll;contentWidth:availableWidth;Layout.fillWidth:true;Layout.fillHeight:true;clip:true
                            ColumnLayout {width:navScroll.availableWidth;spacing:4
                        Repeater {
                            model: app.navItems
                            delegate: Item {
                                required property var modelData
                                objectName: "navigation_" + modelData.key
                                Layout.fillWidth: true
                                Layout.preferredHeight: 46

                                Rectangle {
                                    anchors.fill: parent
                                    radius: 6
                                    color: app.currentPage === modelData.key
                                        ? app.theme.accentSoft : "transparent"
                                }
                                NavigationIcon {name:modelData.key;ink:app.currentPage===modelData.key?app.theme.accent:app.theme.ink;width:24;height:24;anchors.verticalCenter:parent.verticalCenter;x:app.navigationCollapsed?(parent.width-width)/2:10}
                                Column {
                                    visible:!app.navigationCollapsed
                                    anchors.left: parent.left
                                    anchors.leftMargin: 44
                                    anchors.verticalCenter: parent.verticalCenter
                                    spacing: 1
                                    Label {
                                        text: modelData.label
                                        color: app.currentPage === modelData.key
                                            ? app.theme.accent : app.theme.ink
                                        font.pixelSize: 14
                                        font.weight: app.currentPage === modelData.key
                                            ? Font.DemiBold : Font.Normal
                                    }
                                    Label {
                                        text: modelData.hint
                                        color: app.theme.muted
                                        font.pixelSize: 11
                                    }
                                }
                                ToolTip.visible:navMouse.containsMouse
                                ToolTip.text:modelData.label
                                MouseArea {
                                    id:navMouse;hoverEnabled:true
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: app.goTo(modelData.key)
                                }
                            }
                        }

                            }
                        }


                        Label {
                            Layout.fillWidth: true
                            visible:!app.navigationCollapsed
                            text: project.path || ""
                            color: app.theme.muted
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
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

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 54
                        Layout.leftMargin: app.islandInset
                        Layout.rightMargin: app.islandInset
                        Layout.topMargin: app.islandInset
                        color: app.theme.card
                        border.color: app.theme.line
                        radius: app.islandRadius

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 20
                            anchors.rightMargin: 20
                            spacing: 12

                            Label {
                                text: {
                                    for (var i = 0; i < app.navItems.length; ++i)
                                        if (app.navItems[i].key === app.currentPage)
                                            return app.navItems[i].label
                                    return ""
                                }
                                font.pixelSize: 17
                                font.weight: Font.DemiBold
                                color: app.theme.ink
                            }
                            Label {
                                text: project.path || ""
                                color: app.theme.muted
                                font.pixelSize: 11
                                elide: Text.ElideMiddle
                                Layout.maximumWidth: 380
                            }
                            Item { Layout.fillWidth: true }
                            Label {
                                visible: app.busy
                                text: "处理中…"
                                color: app.theme.accent
                                font.pixelSize: 12
                            }
                            Button {
                                objectName: "assistantToggle"
                                text: app.assistantOpen ? "收起协作者" : "协作者"
                                flat: true
                                font.pixelSize: 11
                                onClicked: app.assistantOpen = !app.assistantOpen
                            }
                        }
                    }

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
                        currentIndex: {
                            for (var i = 0; i < app.navItems.length; ++i)
                                if (app.navItems[i].key === app.currentPage) return i
                            return 0
                        }

                        // 每个位置一个 Loader：第一次进入那一页才实例化，之后保留。
                        Loader { id: browseLoader; active: app.pageVisited("browse") || app.currentPage === "browse"; sourceComponent: browsePageComponent }
                        Loader { active: app.pageVisited("relations") || app.currentPage === "relations"; sourceComponent: relationsPageComponent }
                        Loader { id: sourceLoader; active: app.pageVisited("source") || app.currentPage === "source"; sourceComponent: sourcePageComponent }
                        Loader { active: app.pageVisited("adaptation") || app.currentPage === "adaptation"; sourceComponent: adaptationPageComponent }
                        Loader { active: app.pageVisited("assets") || app.currentPage === "assets"; sourceComponent: assetsPageComponent }
                        Loader { active: app.pageVisited("screenplay") || app.currentPage === "screenplay"; sourceComponent: screenplayPageComponent }
                        Loader { active: app.pageVisited("storyboard") || app.currentPage === "storyboard"; sourceComponent: storyboardPageComponent }
                        Loader { active: app.pageVisited("director") || app.currentPage === "director"; sourceComponent: directorPageComponent }
                        Loader { active: app.pageVisited("screenplay_structure") || app.currentPage === "screenplay_structure"; sourceComponent: structurePageComponent }
                        Loader { active: app.pageVisited("static_check") || app.currentPage === "static_check"; sourceComponent: staticCheckPageComponent }
                        Loader { active: app.pageVisited("project_settings") || app.currentPage === "project_settings"; sourceComponent: projectSettingsPageComponent }
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
                    AssistantPanel {
                        id: assistantPanel
                        parent: app.assistantDocked ? assistantDockSlot : assistantOverlay
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        anchors.right: parent.right
                        anchors.margins: app.islandInset
                        width: app.assistantDocked
                            ? parent.width - 2 * app.islandInset
                            : Math.min(360 + (app.islandsMode ? app.theme.island_gap : 0),
                                       parent.width - 2 * app.islandInset)
                        app: app
                        pageId: app.currentPage
                        visible: app.assistantOpen
                        onCloseRequested: app.assistantOpen = false
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 30 + (app.islandsMode ? app.theme.island_gap : 0)
            // 卡片同样画在内层：岛屿模式下状态栏也浮起来，紧凑模式贴边即原样。
            color: "transparent"
            border.width: 0

            Rectangle {
                id: statusCard
                anchors.fill: parent
                anchors.margins: app.islandInset
                color: app.theme.card
                border.color: app.theme.line
                radius: app.islandRadius
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: app.islandInset + 20
                anchors.rightMargin: app.islandInset + 20
                Label {
                    objectName: "statusLine"
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    font.pixelSize: 11
                    // 常驻一行自检：出错/提示优先，否则说明界面自己以为在等什么。
                    // 卡在「处理中」时，这一行就是现场记录。
                    text: app.errorText.length > 0 ? app.errorText
                        : app.notice.length > 0 ? app.notice
                        : app.diagnosticLine
                    color: app.errorText.length > 0 ? app.theme.bad : app.theme.muted
                }
                // 构建身份：截图与 bug 报告靠这一行指回具体代码（真源 narrative/version.py）。
                Label {
                    objectName: "appVersionLabel"
                    text: bridge.appVersion
                    color: app.theme.muted
                    font.pixelSize: 10
                    elide: Text.ElideRight
                    Layout.maximumWidth: 300
                }
                Button {
                    visible: app.errorText.length > 0 || app.notice.length > 0
                    text: "清除"
                    flat: true
                    onClicked: { app.errorText = ""; app.notice = "" }
                }
            }
        }
    }


    // 懒创建用的组件定义。顺序与 navItems 一致，由上面每个 Loader 按需实例化。
    Component { id: browsePageComponent; BrowsePage { app: windowRoot } }
    Component { id: relationsPageComponent; RelationPage { app: windowRoot } }
    Component { id: sourcePageComponent; SourcePage { app: windowRoot } }
    Component { id: adaptationPageComponent; AdaptationPage { app: windowRoot } }
    Component { id: assetsPageComponent; AssetsPage { app: windowRoot } }
    Component { id: screenplayPageComponent; ScreenplayPage { app: windowRoot } }
    Component { id: storyboardPageComponent; StoryboardPage { app: windowRoot } }
    Component { id: directorPageComponent; DirectorDeskPage { app: windowRoot } }
    Component { id: structurePageComponent; ScreenplayStructurePage { app: windowRoot } }
    Component { id: staticCheckPageComponent; StaticCheckPage { app: windowRoot } }
    Component { id: projectSettingsPageComponent; ProjectSettingsPage { app: windowRoot } }
}
