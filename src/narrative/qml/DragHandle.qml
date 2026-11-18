// 拖动把手：在“没有别的控件”的区域拖动窗口。
// 按下去先记位置，真的移动了才交给系统（startSystemMove），这样双击最大化不会被
// 拖动吃掉；双击本身切换最大化。拖到屏幕顶部由系统贴边最大化（Aero Snap）。
import QtQuick

MouseArea {
    id: handle
    property var app
    property real threshold: 4
    property real pressX: 0
    property real pressY: 0
    property bool moving: false

    acceptedButtons: Qt.LeftButton
    onPressed: function (mouse) {
        handle.pressX = mouse.x
        handle.pressY = mouse.y
        handle.moving = false
    }
    onPositionChanged: function (mouse) {
        if (handle.moving || !handle.app) return
        if (Math.abs(mouse.x - handle.pressX) < handle.threshold
                && Math.abs(mouse.y - handle.pressY) < handle.threshold) return
        handle.moving = true
        handle.app.startSystemMove()
    }
    onDoubleClicked: {
        if (!handle.app) return
        handle.app.visibility === Window.Maximized ? handle.app.showNormal() : handle.app.showMaximized()
    }
}
