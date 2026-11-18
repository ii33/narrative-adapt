// 三维预演台：整页承载 DirectorDesk 的构建产物。
// 产物是 ES module，只能通过本地 http 打开；地址与下载落盘由 bridge.previsWorkspace() 给出。
// 注意：QtWebEngineQuick 的 import 只出现在 DirectorWorkspaceView.qml 里，
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

    Rectangle {
        anchors.fill: parent
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
            background: Rectangle { color: root.app.theme.card; border.color: root.app.theme.line }
        }
    }
}
