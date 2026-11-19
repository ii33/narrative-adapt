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

    // 预演接口的引导脚本。接口本体在页面内（dist 里的 host/host-api.ts 挂了
    // window.__narrativePrevisHost），宿主这一侧只做两件事：确认它挂上了、调用它的 boot()
    // 让页面知道“宿主已经接管”。页面还没跑起来时先放一个占位对象，主程序那边拿到的是
    // 可读的 error，而不是 undefined。
    // 注意：整段只用单引号包 JS 字符串，免得在 QML 双引号字符串里出现转义。
    readonly property string hostBootstrap: "(function () {
        var host = window.__narrativePrevisHost;
        if (!host || typeof host.boot !== 'function') {
            var message = '预演接口未加载';
            window.__narrativePrevisHost = {
                boot: function () { return { ok: false, error: message }; },
                hello: function () { return Promise.resolve({ ok: false, error: message }); }
            };
            return 'missing';
        }
        try { return JSON.stringify(host.boot()); } catch (error) { return 'error: ' + error.message; }
    })();"

    // 换肤：把它的中性灰映射到本项目令牌，并注入驱动它的那一小段接口。
    function applyTheme() {
        var script = bridge.previsThemeScript()
        if (script.length > 0)
            runJavaScript(script, function () {
                // 顺序不能反：先主题 CSS/JS（会摘掉顶栏节点），再预演接口的引导，
                // 最后才回调 onReady——既有行为一行没动，只是多了一步确认。
                view.runJavaScript(view.hostBootstrap, function (result) {
                    if (view.onReady !== null) view.onReady()
                })
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
