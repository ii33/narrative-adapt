// 三维预演台：整页承载 DirectorDesk 的构建产物。
// 它自己的顶栏已由 CSS 隐藏，改由这里的一条工具条驱动（场景切换、保存、导出），
// 窗口按钮与工具条同一行——不再单独占一行。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    objectName: "previsPage"
    property var app
    property string workspaceUrl: ""
    property bool available: false
    property string reason: ""
    property bool loading: true
    property string lastNotice: ""
    property var scenes: []
    property string currentScene: ""
    property bool ready: false
    // 可读性四项（键名与页面 setReadability 一致）。页面没有取值接口，状态由桥记着：
    // 这里画的是「桥记的那一份」，页面没就绪时是全开——那是宿主的默认值，不是猜页面。
    property var readability: ({persons: true, roomOutlines: true, kindColors: true, labels: true})
    property string readabilityError: ""
    // previsSceneSet() 的原样回执：状态行用它显示「这页是哪一场」。
    property var sceneSet: ({ok: false, error: "", scene_id: "", name: "",
                             entity_count: 0, camera_count: 0})
    // 铺布景的**就地**回执：成功一句、失败一句，都贴在工具条上；失败不弹窗。
    property string toolbarNote: ""
    property bool toolbarNoteOk: false

    // 镜头表「定位预演」只做两件事：记下这一镜、切页。预演页自己不接场景参数
    // （它从预演工程里列场景），所以这里给一行定位回执——否则用户点了「预演」，
    // 看到的还是上一次的场景，会以为定位没生效。文字由桥按同一份选择记录生成。
    readonly property var selection: (root.app && root.app.storyboardSelection)
                                     ? root.app.storyboardSelection : ({})
    readonly property string selectionText: root.app ? bridge.storyboardSelectionLabel(root.selection) : ""

    // 「当前场」是铺布景的落点。到预演页有两条路（镜头表「定位预演」、分镜稿/剧本站），
    // 两边各记了一份选择；先认镜头表那一份，再退回剧本那一份。都没有就禁用按钮并说明
    // 去哪儿选——预演页自己不接场景参数，不能凭空猜一条场次出来。
    readonly property string currentUnitId: {
        var board = root.selection && root.selection.unit_id ? String(root.selection.unit_id) : ""
        if (board.length > 0) return board
        var script = (root.app && root.app.screenplaySelection) ? root.app.screenplaySelection : ({})
        return script && script.unit_id ? String(script.unit_id) : ""
    }
    readonly property string applySetDisabledReason: "还没有当前场：先在镜头表里选一镜点「预演」，"
                                                     + "或从分镜稿打开本场预演页"
    // 工具条下面那条状态行左侧的字：扫一眼就知道这页是哪一场、里面有多少东西。
    readonly property string sceneSummary: (root.sceneSet && root.sceneSet.ok === true)
        ? "当前场景：" + (root.sceneSet.name || root.sceneSet.scene_id || "未命名")
          + "（" + root.sceneSet.entity_count + " 个对象 / " + root.sceneSet.camera_count + " 台机位）"
        : "预演页未就绪"

    function openWorkspace() {
        var info = bridge.previsWorkspace()
        available = info.available === true
        reason = info.reason || ""
        workspaceUrl = info.url || ""
        loading = available
    }

    // 场景列表是它自己异步建出来的：注入完成时可能还是空的，过一会儿再看一次。
    function scheduleSceneRefresh() {
        refreshScenes()
        sceneRetry.restart()
    }

    function refreshScenes() {
        var loader = loaderItem.item
        if (!loader) return
        loader.runJavaScript("JSON.stringify(window.__narrativePrevis ? window.__narrativePrevis.scenes() : [])", function (raw) {
            try { root.scenes = JSON.parse(raw || "[]") } catch (error) { root.scenes = [] }
            currentScene = ""
            root.refreshCurrent()
        })
    }

    function refreshCurrent() {
        var loader = loaderItem.item
        if (!loader) return
        loader.runJavaScript("window.__narrativePrevis ? window.__narrativePrevis.current() : ''", function (value) {
            root.currentScene = value || ""
        })
    }

    function selectScene(value) {
        var loader = loaderItem.item
        if (!loader) return
        loader.runJavaScript("window.__narrativePrevis ? window.__narrativePrevis.select(" + JSON.stringify(value) + ") : false")
        root.currentScene = value
    }

    function act(name) {
        var loader = loaderItem.item
        if (!loader) return
        loader.runJavaScript("window.__narrativePrevis ? window.__narrativePrevis.act(" + JSON.stringify(name) + ") : false")
    }

    // ---- 工具条：可读性开关与铺布景 ------------------------------------------
    // 这两件事原先只能绕到镜头表或助手去做。函数都放在页面这一层（而不是按钮的
    // onClicked 里），因为「铺布景」还要配一次场景刷新，逻辑塞进点击处理就没人复用。
    function flagsOf(source) {
        var data = source || ({})
        return {persons: data.persons === true, roomOutlines: data.roomOutlines === true,
                kindColors: data.kindColors === true, labels: data.labels === true}
    }

    function syncReadability() {
        var flags = bridge.previsReadability() || ({})
        root.readability = root.flagsOf(flags)
        root.readabilityError = flags.error ? String(flags.error) : ""
    }

    // 点一下就下发：成功用桥/页面回执里的状态重画（页面可能归一化过），
    // 失败只把原因贴在面板底部——不弹窗，因为开关点坏了不算事故。
    function setReadability(patch) {
        var keys = ["persons", "roomOutlines", "kindColors", "labels"]
        var next = root.flagsOf(root.readability)
        for (var index = 0; index < keys.length; ++index)
            if (patch && patch[keys[index]] !== undefined) next[keys[index]] = patch[keys[index]] === true
        var result = bridge.setPrevisReadability(next) || ({})
        if (result.ok === true) {
            root.readability = root.flagsOf(result.applied)
            root.readabilityError = ""
        } else {
            root.readabilityError = String(result.error || "没能改可读性开关")
            // 没改成就要把开关拨回去：换成一个新对象，四个复选框的绑定因此重算一次
            // （属性值没变的话绑定不会重跑，界面会停在用户刚拨过去的位置上，骗人）。
            root.readability = root.flagsOf(root.readability)
        }
    }

    function refreshSceneSet() {
        root.sceneSet = bridge.previsSceneSet() || ({})
    }

    // 铺布景：没有当前场时根本不发请求（按钮已禁用，这里再兜一次，
    // 免得将来有快捷键或其他入口绕过 enabled）。
    function applySet() {
        if (root.currentUnitId.length === 0) {
            root.toolbarNote = root.applySetDisabledReason
            root.toolbarNoteOk = false
            return
        }
        var result = bridge.applySceneSet(root.currentUnitId) || ({})
        if (result.ok === true) {
            var blocks = result.blocks ? result.blocks.length : 0
            var cameras = result.cameras ? result.cameras.length : 0
            root.toolbarNote = "已铺 " + blocks + " 个盒体、" + cameras + " 台机位"
            root.toolbarNoteOk = true
            root.refreshSceneSet()   // 铺完这页的场景变了：下面那句摘要立刻跟上
        } else {
            root.toolbarNote = String(result.error || "铺布景失败")
            root.toolbarNoteOk = false
        }
    }

    Timer {
        id: sceneRetry
        interval: 1500
        repeat: false
        onTriggered: {
            root.refreshScenes()
            root.refreshSceneSet()
        }
    }

    Component.onCompleted: openWorkspace()

    Connections {
        target: bridge
        function onNoticeRaised(message) { lastNotice = message }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // 工具条：场景切换 + 保存/导出，右侧同一行是窗口按钮。
        Rectangle {
            id: previsBar
            objectName: "previsBar"
            Layout.fillWidth: true
            Layout.preferredHeight: 40
            // 透明：底色就是窗口底色，不另调一个颜色，避免和窗口差一档。
            color: "transparent"

            DragHandle {
                app: root.app
                anchors.fill: parent
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 128
                spacing: 6

                ComboBox {
                    objectName: "previsScene"
                    implicitWidth: 220
                    implicitHeight: 24
                    font.pixelSize: 12
                    model: root.scenes
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: root.scenes.findIndex(function (scene) { return scene.value === root.currentScene })
                    displayText: root.scenes.length ? currentText : "（未加载）"
                    enabled: root.scenes.length > 0
                    onActivated: root.selectScene(model[currentIndex].value)
                }
                ToolButton {
                    objectName: "previsSave"
                    text: "保存"
                    font.pixelSize: 12
                    implicitHeight: 24
                    enabled: root.ready
                    onClicked: root.act("save")
                }
                ToolButton {
                    objectName: "previsExport"
                    text: "导出"
                    font.pixelSize: 12
                    implicitHeight: 24
                    enabled: root.ready
                    onClicked: root.act("export")
                }
                // 可读性开关：直接开一个小面板，不用再绕到助手/镜头表。
                ToolButton {
                    id: readabilityButton
                    objectName: "previsReadability"
                    text: "可读性"
                    font.pixelSize: 12
                    implicitHeight: 24
                    // 开关是写给页面看的：页面没接管之前点了也没人接，直接禁用。
                    enabled: root.ready
                    onClicked: {
                        root.syncReadability()
                        readabilityPanel.open()
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: root.ready
                        ? "画面可读性：人物 / 房间轮廓 / 分类配色 / 名称标签"
                        : "预演页还没就绪，可读性开关暂时不可用"
                }
                // 铺布景：把本场绑定的地点布景铺进这页。没有当前场就不让点，
                // 光标停上去说明去哪儿选一场——否则用户只会看到按钮是灰的。
                ToolButton {
                    objectName: "previsApplySet"
                    text: "铺布景"
                    font.pixelSize: 12
                    implicitHeight: 24
                    enabled: root.currentUnitId.length > 0
                    onClicked: root.applySet()
                    ToolTip.visible: hovered
                    ToolTip.text: root.currentUnitId.length > 0
                        ? "把本场绑定的地点布景铺进预演页（只增改，不删页面里已有的东西）"
                        : root.applySetDisabledReason
                }
                // 铺布景的回执不在这行：这条工具条右边是窗口按钮，回执一长就会压到
                // 它们身上。它放到下面那条状态行（工具条与定位回执之间）。
                Item { Layout.fillWidth: true }
            }

            WindowChrome {
                id: controls
                app: root.app
                anchors.top: parent.top
                anchors.topMargin: 12
                anchors.right: parent.right
                anchors.rightMargin: 12
            }
        }

        // 状态行：左边「这页是哪一场」，右边「上一次铺布景的结果」。
        // 为什么单占一行而不塞进工具条：工具条右侧的宽度是留给窗口按钮的，
        // 回执一长（失败原因常常很长）就会压到窗口按钮上；这一行整条归文字用，
        // 失败原因能完整显示，也就不用弹窗去打断用户。
        Rectangle {
            objectName: "previsStatusBar"
            Layout.fillWidth: true
            Layout.preferredHeight: 24
            color: "transparent"

            DragHandle {
                app: root.app
                anchors.fill: parent
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 12

                // 这页是哪一场：数据来自 previsSceneSet()，页面没就绪时说「未就绪」，
                // 不留一行空白让人以为场景是空的。
                Label {
                    objectName: "previsSceneSummary"
                    text: root.sceneSummary
                    font.pixelSize: 11
                    color: root.app.theme.muted
                    Layout.maximumWidth: 320
                    Layout.minimumWidth: 0
                    elide: Text.ElideRight
                    HoverHandler { id: summaryHover }
                    ToolTip.visible: summaryHover.hovered && (root.sceneSet.error || "").length > 0
                    ToolTip.text: root.sceneSet.error || ""
                }
                // 铺布景的回执：成功一句、失败一句，就地显示。不弹窗——一次点击不值得
                // 打断整页，而失败原因比「失败」两个字有用得多。
                Label {
                    objectName: "previsToolbarNote"
                    visible: root.toolbarNote.length > 0
                    text: root.toolbarNote
                    font.pixelSize: 11
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    elide: Text.ElideRight
                    horizontalAlignment: Text.AlignRight
                    color: root.toolbarNoteOk ? root.app.theme.muted : root.app.theme.warn
                    HoverHandler { id: noteHover }
                    ToolTip.visible: noteHover.hovered && text.length > 0
                    ToolTip.text: root.toolbarNote
                }
            }
        }

        // 定位回执：只在从镜头表「定位预演」切过来时出现。
        // 一行字，不占操作区；没有定位就不显示，不留一条空条。
        Rectangle {
            objectName: "previsLocationBar"
            Layout.fillWidth: true
            Layout.preferredHeight: 24
            visible: root.selectionText.length > 0
            color: "transparent"

            DragHandle {
                app: root.app
                anchors.fill: parent
            }

            Label {
                objectName: "previsLocationReceipt"
                anchors.left: parent.left
                anchors.leftMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                text: root.selectionText
                color: root.app.theme.muted
                font.pixelSize: 11
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: root.app.theme.paper

            Loader {
                id: loaderItem
                anchors.fill: parent
                active: root.available && root.workspaceUrl.length > 0
                source: "DirectorWorkspaceView.qml"
                onLoaded: {
                    item.app = root.app
                    item.initialUrl = root.workspaceUrl
                    item.onReady = function () {
                        root.ready = true
                        // 状态只在页面就绪之后再读：之前读到的一定是「未就绪」，
                        // 先画一遍等于先骗一次用户。
                        root.syncReadability()
                        root.refreshSceneSet()
                        root.scheduleSceneRefresh()
                    }
                }
            }

            // 不可用时不静默留白：说明缺什么、怎么办。
            ColumnLayout {
                visible: !root.available
                anchors.centerIn: parent
                spacing: 10
                width: Math.min(parent.width - 80, 640)
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: "三维预演台不可用"
                    font.pixelSize: 18
                    font.bold: true
                    color: root.app.theme.ink
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: root.reason
                    color: root.app.theme.muted
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: "把前端产物放到工程目录的 previs-web/，或设置环境变量 NARRATIVE_PREVIS_DIST 指向构建输出。"
                    color: root.app.theme.muted
                }
                Button {
                    text: "重新检测"
                    onClicked: root.openWorkspace()
                }
            }

            Label {
                visible: root.lastNotice.length > 0
                anchors.left: parent.left
                anchors.bottom: parent.bottom
                anchors.margins: 8
                padding: 6
                text: root.lastNotice
                color: root.app.theme.ink
                background: Rectangle {
                    color: root.app.theme.card
                    border.color: root.app.theme.line
                }
            }
        }
    }

    // 可读性小面板：四个开关 + 全开/全关，贴着「可读性」按钮弹出。
    // 为什么不做成整行：工具条已经有六个控件，再加四个开关会把场景下拉挤没，
    // 而且这四项是「调一下就不动」的设置，收进面板比常驻更合适。
    Popup {
        id: readabilityPanel
        objectName: "previsReadabilityPopup"
        // 贴着按钮左缘弹出，但不越过右边界——右边那一块是窗口按钮的位置，别抢。
        x: Math.max(12, Math.min(readabilityButton.x, root.width - width - 12))
        y: previsBar.height + 4
        width: 196
        padding: 10
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle {
            color: root.app.theme.card
            border.color: root.app.theme.line
            radius: 6
        }
        contentItem: ColumnLayout {
            spacing: 4
            Label {
                text: "画面可读性"
                font.pixelSize: 12
                color: root.app.theme.ink
            }
            CheckBox {
                objectName: "previsReadabilityPersons"
                text: "人物"
                font.pixelSize: 12
                checked: root.readability.persons
                onToggled: root.setReadability({persons: checked})
            }
            CheckBox {
                objectName: "previsReadabilityRoomOutlines"
                text: "房间轮廓"
                font.pixelSize: 12
                checked: root.readability.roomOutlines
                onToggled: root.setReadability({roomOutlines: checked})
            }
            CheckBox {
                objectName: "previsReadabilityKindColors"
                text: "分类配色"
                font.pixelSize: 12
                checked: root.readability.kindColors
                onToggled: root.setReadability({kindColors: checked})
            }
            CheckBox {
                objectName: "previsReadabilityLabels"
                text: "名称标签"
                font.pixelSize: 12
                checked: root.readability.labels
                onToggled: root.setReadability({labels: checked})
            }
            RowLayout {
                spacing: 6
                Button {
                    objectName: "previsReadabilityAll"
                    text: "全开"
                    font.pixelSize: 11
                    implicitHeight: 22
                    onClicked: root.setReadability({persons: true, roomOutlines: true,
                                                    kindColors: true, labels: true})
                }
                Button {
                    objectName: "previsReadabilityNone"
                    text: "全关"
                    font.pixelSize: 11
                    implicitHeight: 22
                    onClicked: root.setReadability({persons: false, roomOutlines: false,
                                                    kindColors: false, labels: false})
                }
            }
            // 改不成的原因就贴在面板底部（页面未就绪、调用超时…），不弹窗。
            Label {
                objectName: "previsReadabilityError"
                visible: root.readabilityError.length > 0
                text: root.readabilityError
                color: root.app.theme.warn
                font.pixelSize: 11
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
    }
}
