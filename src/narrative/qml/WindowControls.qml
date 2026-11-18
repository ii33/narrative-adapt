// 窗口按钮：最小化 / 最大化（或还原）/ 关闭。
// 没有系统标题栏，这三个按钮直接长在页面自己的顶栏里，不再有一条独立的标题栏。
import QtQuick
import QtQuick.Controls

Row {
    id: controls
    property var app
    property color ink: app ? app.theme.muted : "#93a3b8"
    spacing: 2

    Repeater {
        model: [
            {glyph: "—", hint: "最小化"},
            {glyph: "□", hint: "最大化"},
            {glyph: "✕", hint: "关闭"}
        ]
        delegate: Button {
            required property var modelData
            required property int index
            objectName: "windowControl" + index
            text: modelData.glyph
            flat: true
            font.pixelSize: 12
            implicitWidth: 34
            implicitHeight: 24
            palette.buttonText: controls.ink
            ToolTip.visible: hovered
            ToolTip.text: modelData.hint
            onClicked: {
                if (index === 0) controls.app.showMinimized()
                else if (index === 1)
                    controls.app.visibility === Window.Maximized
                        ? controls.app.showNormal() : controls.app.showMaximized()
                else controls.app.close()
            }
        }
    }
}
