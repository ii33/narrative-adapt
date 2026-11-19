// 三维预演台：整页承载 DirectorDesk 的构建产物。
// 它自己的顶栏已由 CSS 隐藏，改由这里的一条工具条驱动（场景切换、保存、导出），
// 窗口按钮与工具条同一行——不再单独占一行。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var app
    property string workspaceUrl: ""
    property bool available: false
    property string reason: ""
    property bool loading: true
    property string lastNotice: ""
    property var scenes: []
    property string currentScene: ""
    property bool ready: false

    // 镜头表「定位预演」只做两件事：记下这一镜、切页。预演页自己不接场景参数
    // （它从预演工程里列场景），所以这里给一行定位回执——否则用户点了「预演」，
    // 看到的还是上一次的场景，会以为定位没生效。文字由桥按同一份选择记录生成。
    readonly property var selection: (root.app && root.app.storyboardSelection)
                                     ? root.app.storyboardSelection : ({})
    readonly property string selectionText: root.app ? bridge.storyboardSelectionLabel(root.selection) : ""

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

    Timer {
        id: sceneRetry
        interval: 1500
        repeat: false
        onTriggered: root.refreshScenes()
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
}
