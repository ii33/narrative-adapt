// 每个页面顶行末尾都放这一组：协作者开关 + 窗口按钮。
// 统一放在一个组件里，各页只需要把 WindowChrome 放在自己标题行（RowLayout）的最后一项，
// 位置、间距、顺序就完全一致，不会再出现“有的页在标题行、有的页在别的行”。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Row {
    id: chrome
    property var app
    spacing: 6
    // 顶对齐：各页标题行高度不同，居中对齐会让这组控件的纵向位置每页差几像素。
    Layout.alignment: Qt.AlignTop

    Button {
        objectName: "assistantToggle"
        text: chrome.app && chrome.app.assistantOpen ? "收起协作者" : "协作者"
        flat: true
        font.pixelSize: 11
        implicitHeight: 24
        onClicked: if (chrome.app) chrome.app.assistantOpen = !chrome.app.assistantOpen
    }

    WindowControls { app: chrome.app }
}
