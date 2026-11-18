// 三维预演台：整页承载 DirectorDesk 的构建产物。
// 产物是 ES module，只能通过本地 http 打开；地址与下载落盘由 bridge.previsWorkspace() 给出。
// 注意：WebEngine 的 QML import 只出现在 DirectorWorkspaceView.qml 里，
// 由下面的 Loader 按需加载——否则没有 WebEngine 的环境连本页都解析不了。
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

    function openWorkspace() {
        var info = bridge.previsWorkspace()
        available = info.available === true
        reason = info.reason || ""
        workspaceUrl = info.url || ""
        loading = available
    }

    Component.onCompleted: openWorkspace()

    Connections {
        target: bridge
        function onNoticeRaised(message) { lastNotice = message }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // 预演页自己的顶栏：返回、页名、拖动区、窗口按钮。
        // 没有独立的系统标题栏，也没有单独的窗口条——按钮就长在这一行里。
        Rectangle {
            id: previsBar
            objectName: "previsBar"
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            color: root.app.theme.paper

            MouseArea {
                anchors.left: parent.left
                anchors.right: controls.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                acceptedButtons: Qt.LeftButton
                onPressed: root.app.startSystemMove()
                onDoubleClicked: root.app.visibility === Window.Maximized
                    ? root.app.showNormal() : root.app.showMaximized()
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 6
                anchors.rightMargin: 6
                spacing: 8

                ToolButton {
                    objectName: "previsBack"
                    text: "←"
                    font.pixelSize: 15
                    onClicked: root.app.goTo("storyboard")
                    ToolTip.visible: hovered
                    ToolTip.text: "回到分镜稿"
                }
                NavigationIcon {
                    name: "previs"
                    ink: root.app.theme.ink
                    width: 18
                    height: 18
                }
                Label {
                    text: "预演"
                    color: root.app.theme.ink
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                }
                Item { Layout.fillWidth: true }
            }

            WindowControls {
                id: controls
                app: root.app
                anchors.verticalCenter: parent.verticalCenter
                anchors.right: parent.right
                anchors.rightMargin: 6
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: root.app.theme.paper

            Loader {
                anchors.fill: parent
                active: root.available && root.workspaceUrl.length > 0
                source: "DirectorWorkspaceView.qml"
                onLoaded: {
                    item.app = root.app
                    item.initialUrl = root.workspaceUrl
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
