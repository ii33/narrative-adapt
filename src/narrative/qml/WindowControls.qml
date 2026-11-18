// 窗口按钮：最小化 / 最大化（或还原）/ 关闭。
// 没有系统标题栏，这三个按钮长在页面自己的顶行里，底色跟着窗口走（透明 + 悬停高亮）。
//
// 这里不用 Qt Quick Controls 的 Button：本应用用的是原生（Windows）样式，
// 该样式不支持自定义 background / contentItem，写上去会被忽略甚至画出一块系统底。
// 用 Rectangle + Text + MouseArea 自己画，任何样式下表现一致。
import QtQuick
import QtQuick.Controls

Row {
    id: controls
    property var app
    readonly property var palette: app ? app.theme : null
    property color ink: palette ? palette.muted : "#93a3b8"
    spacing: 2

    Repeater {
        model: [
            {glyph: "—", hint: "最小化"},
            {glyph: "□", hint: "最大化"},
            {glyph: "✕", hint: "关闭"}
        ]
        delegate: Rectangle {
            id: button
            required property var modelData
            required property int index
            objectName: "windowControl" + index
            implicitWidth: 34
            implicitHeight: 24
            radius: 3
            color: mouse.containsMouse
                ? (controls.palette ? controls.palette.hover : "#232f3d")
                : "transparent"

            Text {
                anchors.centerIn: parent
                text: button.modelData.glyph
                font.pixelSize: 12
                renderType: Text.NativeRendering
                color: mouse.containsMouse
                    ? (controls.palette ? controls.palette.ink : "#e8eef7")
                    : controls.ink
            }

            MouseArea {
                id: mouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    if (!controls.app) return
                    if (button.index === 0) controls.app.showMinimized()
                    else if (button.index === 1)
                        controls.app.visibility === Window.Maximized
                            ? controls.app.showNormal() : controls.app.showMaximized()
                    else controls.app.close()
                }
            }
            ToolTip.visible: mouse.containsMouse
            ToolTip.text: button.modelData.hint
        }
    }
}
