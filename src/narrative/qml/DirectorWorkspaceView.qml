// 单独一个文件：WebEngine 的 QML import 只在这里出现（PySide6 6.12 的模块名是 QtWebEngine）。
// 若运行环境没有 WebEngine，Load 这个文件会失败，Loader 报 Error——但 Main.qml 与
// 其他页面照常工作（这正是把它拆出来的原因）。
import QtQuick
import QtWebEngine

WebEngineView {
    id: view
    property var app
    property string initialUrl: ""
    property bool offline: false

    url: initialUrl
    backgroundColor: "#101214"

    onLoadingChanged: function(request) {
        if (request.status === WebEngineView.LoadFailedStatus) {
            offline = true
        } else if (request.status === WebEngineView.LoadSucceededStatus) {
            offline = false
        }
    }
}
