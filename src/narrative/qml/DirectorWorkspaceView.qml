// 单独一个文件：WebEngine 的 QML import 只在这里出现（PySide6 6.12 的模块名是 QtWebEngine）。
// 若运行环境没有 WebEngine，加载这个文件会失败，Loader 报 Error——但 Main.qml 与
// 其他页面照常工作（这正是把它拆出来的原因）。
import QtQuick
import QtWebEngine

WebEngineView {
    id: view
    property var app
    property string initialUrl: ""
    property bool offline: false
    // 页面就绪（脚本注入完成）后回调：工具条据此拉场景列表。
    property var onReady: null

    url: initialUrl
    backgroundColor: "#101620"

    // 换肤：把它的中性灰映射到本项目令牌，并注入驱动它的那一小段接口。
    function applyTheme() {
        var script = bridge.previsThemeScript()
        if (script.length > 0)
            runJavaScript(script, function () {
                if (view.onReady !== null) view.onReady()
            })
    }

    onLoadingChanged: function(request) {
        if (request.status === WebEngineView.LoadFailedStatus) {
            offline = true
        } else if (request.status === WebEngineView.LoadSucceededStatus) {
            offline = false
            applyTheme()
        }
    }
}
