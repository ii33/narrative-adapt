import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 全局设置里的「MCP 服务器」：跨工程共用的外部工具服务器清单。
//
// 面板自己不留第二份真状态：列表永远来自 bridge.mcpServers()（配置文件 + 缓存里的工具），
// 编辑器里那份是可丢弃的草稿，按「保存」才整份写回。这样手改了 JSON、或别处改了配置，
// 重开面板看到的就是磁盘上的样子。
ColumnLayout {
    id: panel
    objectName: "mcpSettingsPanel"

    property var app
    readonly property var t: app ? app.theme : bridge.theme

    property var servers: []
    property string configPath: ""
    property string cachePath: ""
    property string loadError: ""
    property string startupError: ""
    property string message: ""
    property bool messageIsError: false
    // 后台刷新在跑：按钮与行内编辑一起停住，避免同一份配置被两个刷新线程撕开。
    property bool refreshing: false
    property bool testing: false
    // 保存失败过：编辑器重新打开时把草稿原样还给用户，别让他重敲一遍。
    property bool keepDraft: false
    property int editing: -1

    // 编辑草稿。都是独立属性而不是一个对象：QML 只会对「整个 var 属性被替换」发通知，
    // 改对象里的字段不会刷新绑定。
    property string draftName: ""
    property string draftTransport: "stdio"
    property bool draftEnabled: true
    property string draftCommand: ""
    property string draftArgs: ""
    property string draftEnv: ""
    property string draftCwd: ""
    property string draftUrl: ""
    property string draftTimeout: "20"

    readonly property var transportOptions: ["stdio", "http"]
    readonly property bool draftIsStdio: draftTransport !== "http"

    spacing: 8

    Component.onCompleted: panel.reload()
    onVisibleChanged: if (visible) panel.reload()

    function reload() {
        var snapshot = bridge.mcpServers()
        panel.servers = snapshot.servers || []
        panel.configPath = snapshot.path || ""
        panel.cachePath = snapshot.cache_path || ""
        panel.loadError = snapshot.error || ""
        panel.startupError = snapshot.startup_error || ""
    }

    function toolCount(server) {
        return (server.tools || []).length
    }

    function toolTip(server) {
        var tools = server.tools || []
        if (tools.length === 0) return "还没有发现工具：点「测试连接」。"
        var lines = []
        for (var i = 0; i < tools.length; ++i)
            lines.push(tools[i].name + (tools[i].writes ? "（写）" : "（只读）"))
        return lines.join("\n")
    }

    function statusText(server) {
        if (server.enabled === false) return "已停用"
        var count = panel.toolCount(server)
        return count > 0 ? count + " 个工具" : "还没有工具"
    }

    function rowError(server) {
        // 上次刷新的失败原因优先：它比配置类静态说明更接近用户刚做的事。
        if (server.last_error) return server.last_error
        return server.error || ""
    }

    function enabledCount() {
        var count = 0
        for (var i = 0; i < panel.servers.length; ++i)
            if (panel.servers[i].enabled !== false) count += 1
        return count
    }

    function failedCount() {
        var count = 0
        for (var i = 0; i < panel.servers.length; ++i)
            if (panel.servers[i].enabled !== false && panel.servers[i].last_error) count += 1
        return count
    }

    function refreshSummary() {
        var tools = 0
        for (var i = 0; i < panel.servers.length; ++i)
            if (panel.servers[i].enabled !== false) tools += panel.toolCount(panel.servers[i])
        var failed = panel.failedCount()
        return "工具已刷新：" + panel.enabledCount() + " 台启用，共 " + tools + " 个工具"
            + (failed > 0 ? "；" + failed + " 台报错，见下面各行的说明" : "")
    }

    // 逐台结果：测试连接要能一眼看出哪台通了、哪台没通，而不是只回一句「刷新完成」。
    function testReport() {
        var lines = []
        for (var i = 0; i < panel.servers.length; ++i) {
            var server = panel.servers[i]
            var state = server.enabled === false
                ? "已停用"
                : server.last_error
                    ? "失败：" + server.last_error
                    : "正常，" + panel.toolCount(server) + " 个工具"
            lines.push(server.name + " · " + server.transport + " · " + state)
        }
        return lines.length > 0 ? lines.join("\n") : "还没有配置服务器。"
    }

    // -- 数据形状 -------------------------------------------------------------
    // 保存一律传「整份清单」：桥那边按一份完整配置去校验、原子写、再刷新，
    // 不搞增量补丁，界面和磁盘之间不会有第三种状态。

    function serverPayload(server) {
        return {
            name: server.name || "",
            enabled: server.enabled !== false,
            transport: server.transport === "http" ? "http" : "stdio",
            command: server.command || "",
            args: server.args || [],
            env: server.env || ({}),
            cwd: server.cwd || "",
            url: server.url || "",
            timeout: server.timeout === undefined || server.timeout === null ? 20 : server.timeout
        }
    }

    function draftPayload() {
        // 参数与环境变量直接交文本：怎么切分只由桥决定（与配置文件同一个解析口径）。
        return {
            name: panel.draftName.trim(),
            enabled: panel.draftEnabled,
            transport: panel.draftTransport,
            command: panel.draftCommand.trim(),
            args: panel.draftArgs,
            env: panel.draftEnv,
            cwd: panel.draftCwd.trim(),
            url: panel.draftUrl.trim(),
            timeout: panel.draftTimeout.trim() === "" ? 20 : panel.draftTimeout.trim()
        }
    }

    function payloadsReplacing(index, payload) {
        var rows = []
        for (var i = 0; i < panel.servers.length; ++i)
            rows.push(i === index ? payload : panel.serverPayload(panel.servers[i]))
        if (index < 0) rows.push(payload)
        return rows
    }

    // -- 交互 -----------------------------------------------------------------

    function openEditor(index) {
        panel.editing = index
        var server = index >= 0 && index < panel.servers.length
            ? panel.servers[index]
            : ({transport: "stdio", enabled: true, timeout: 20})
        panel.draftName = server.name || ""
        panel.draftTransport = server.transport === "http" ? "http" : "stdio"
        panel.draftEnabled = server.enabled !== false
        panel.draftCommand = server.command || ""
        panel.draftArgs = (server.args || []).join(" ")
        panel.draftEnv = panel.envText(server.env || {})
        panel.draftCwd = server.cwd || ""
        panel.draftUrl = server.url || ""
        panel.draftTimeout = String(server.timeout || 20)
        panel.keepDraft = false
        editor.open()
    }

    function reopenEditor() {
        // 只重开，不重置草稿：保存失败时用户刚填的内容还在。
        panel.keepDraft = false
        editor.open()
    }

    function envText(env) {
        var lines = []
        for (var key in env) lines.push(key + "=" + env[key])
        return lines.join("\n")
    }

    function commitEditor() {
        var payload = panel.draftPayload()
        editor.close()
        panel.submit(panel.payloadsReplacing(panel.editing, payload), true)
    }

    function toggleServer(index, enabled) {
        var server = panel.serverPayload(panel.servers[index])
        server.enabled = enabled
        panel.submit(panel.payloadsReplacing(index, server), false)
    }

    function removeServer(index) {
        var rows = []
        for (var i = 0; i < panel.servers.length; ++i)
            if (i !== index) rows.push(panel.serverPayload(panel.servers[i]))
        panel.submit(rows, false)
    }

    function submit(rows, editing) {
        panel.refreshing = true
        panel.testing = false
        panel.keepDraft = editing
        panel.messageIsError = false
        panel.message = "正在保存并后台刷新工具…"
        // 保存是同步的（小文件原子写），刷新在桥的后台线程里；结果靠 mcpServersChanged 回来。
        bridge.saveMcpServers(rows)
    }

    function refresh(test) {
        panel.testing = test
        panel.refreshing = true
        panel.messageIsError = false
        panel.message = test ? "正在逐台连接，可能要等十几秒…" : "正在后台刷新工具…"
        bridge.refreshMcpServers()
    }

    Connections {
        target: bridge
        function onMcpServersChanged() {
            panel.reload()
            panel.refreshing = false
            panel.keepDraft = false
            if (panel.loadError.length > 0) {
                panel.messageIsError = true
                panel.message = panel.loadError
                return
            }
            panel.messageIsError = panel.failedCount() > 0 && panel.testing
            panel.message = panel.testing ? panel.testReport() : panel.refreshSummary()
            if (!panel.testing) panel.app.toast(panel.message, panel.messageIsError)
            panel.testing = false
        }
        function onErrorOccurred(kind, text) {
            if (kind !== "mcp") return
            panel.refreshing = false
            panel.messageIsError = true
            panel.message = text
            // 保存被拒（名字重复、命令为空、引号没配对…）：把编辑框重新打开，草稿还在。
            if (panel.keepDraft) panel.reopenEditor()
        }
    }

    // -- 头部：标题、按钮、配置文件位置 ---------------------------------------

    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Label {
            text: "MCP 服务器"
            font.pixelSize: 13
            font.weight: Font.DemiBold
            color: panel.t.ink
        }
        Label {
            text: panel.servers.length + " 台"
            color: panel.t.muted
            font.pixelSize: 11
        }
        Item { Layout.fillWidth: true }
        Button {
            objectName: "mcpAddServer"
            text: "新增"
            enabled: !panel.refreshing && panel.loadError.length === 0
            onClicked: panel.openEditor(-1)
        }
        Button {
            objectName: "mcpRefreshServers"
            text: "刷新工具"
            enabled: !panel.refreshing && panel.servers.length > 0
            onClicked: panel.refresh(false)
        }
        Button {
            objectName: "mcpTestServers"
            text: panel.refreshing ? "测试中…" : "测试连接"
            enabled: !panel.refreshing && panel.servers.length > 0
            onClicked: panel.refresh(true)
        }
    }

    Label {
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        color: panel.t.muted
        font.pixelSize: 11
        text: "外部服务器的工具会按 mcp__服务器__工具 的名字进模型工具表，并带上原来的参数说明。"
            + "这里配置的是所有工程共用的，不写进工程文件。"
    }
    Label {
        objectName: "mcpConfigPath"
        Layout.fillWidth: true
        text: "配置文件：" + panel.configPath
        color: panel.t.muted
        font.pixelSize: 10
        elide: Text.ElideMiddle
        ToolTip.visible: pathHover.hovered
        // 缓存也写在附近：清缓存等于「下次启动列表里没有工具」，让人知道它在哪。
        ToolTip.text: panel.configPath + "\n工具缓存：" + panel.cachePath
        HoverHandler { id: pathHover }
    }
    Label {
        objectName: "mcpLoadError"
        Layout.fillWidth: true
        visible: text.length > 0
        text: panel.loadError
        color: panel.t.bad
        font.pixelSize: 11
        wrapMode: Text.Wrap
    }
    Label {
        objectName: "mcpStartupError"
        Layout.fillWidth: true
        visible: text.length > 0
        text: "上次启动读取外部工具时失败：" + panel.startupError
        color: panel.t.muted
        font.pixelSize: 10
        wrapMode: Text.Wrap
    }

    // -- 服务器列表 -----------------------------------------------------------

    Rectangle {
        Layout.fillWidth: true
        Layout.fillHeight: true
        color: panel.t.card
        border.color: panel.t.line
        radius: panel.t.radius

        ListView {
            id: serverList
            objectName: "mcpServerList"
            anchors.fill: parent
            anchors.margins: 6
            clip: true
            spacing: 4
            model: panel.servers

            delegate: Rectangle {
                required property int index
                required property var modelData

                width: ListView.view.width
                // 行高跟着内容走：有错误要显示时多一行，不能把错误挤进固定高度里。
                height: rowBody.implicitHeight + 12
                radius: 6
                color: rowHover.hovered ? panel.t.accentSoft : "transparent"

                HoverHandler { id: rowHover }

                ColumnLayout {
                    id: rowBody
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: 8
                    anchors.rightMargin: 6
                    spacing: 2

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Label {
                            objectName: "mcpServerRowName"
                            Layout.fillWidth: true
                            text: modelData.name
                            color: panel.t.ink
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Label {
                            objectName: "mcpServerRowTransport"
                            text: modelData.transport
                            color: panel.t.muted
                            font.pixelSize: 11
                        }
                        Label {
                            objectName: "mcpServerRowTools"
                            text: panel.statusText(modelData)
                            color: modelData.enabled === false ? panel.t.muted
                                : panel.toolCount(modelData) > 0 ? panel.t.ink : panel.t.muted
                            font.pixelSize: 11
                            ToolTip.visible: toolsHover.hovered
                            ToolTip.text: panel.toolTip(modelData)
                            HoverHandler { id: toolsHover }
                        }
                        Switch {
                            objectName: "mcpServerRowEnabled"
                            checked: modelData.enabled !== false
                            enabled: !panel.refreshing
                            onToggled: panel.toggleServer(index, checked)
                        }
                        Label {
                            objectName: "mcpServerRowEdit"
                            text: "编辑"
                            color: panel.refreshing ? panel.t.muted : panel.t.accent
                            font.pixelSize: 11
                            font.underline: true
                            MouseArea {
                                anchors.fill: parent
                                enabled: !panel.refreshing
                                cursorShape: Qt.PointingHandCursor
                                onClicked: panel.openEditor(index)
                            }
                        }
                        Label {
                            objectName: "mcpServerRowDelete"
                            text: "删除"
                            color: panel.refreshing ? panel.t.muted : panel.t.bad
                            font.pixelSize: 11
                            font.underline: true
                            MouseArea {
                                anchors.fill: parent
                                enabled: !panel.refreshing
                                cursorShape: Qt.PointingHandCursor
                                onClicked: panel.removeServer(index)
                            }
                        }
                    }
                    Label {
                        objectName: "mcpServerRowError"
                        Layout.fillWidth: true
                        visible: panel.rowError(modelData).length > 0
                        text: panel.rowError(modelData)
                        color: panel.t.bad
                        font.pixelSize: 10
                        wrapMode: Text.Wrap
                        // 失败原因可能很长（带命令名与建议）；这行只给两行，全文放提示气泡里，
                        // 否则一台服务器就把整份列表挤没了。
                        maximumLineCount: 2
                        elide: Text.ElideRight
                        ToolTip.visible: errorHover.hovered
                        ToolTip.text: panel.rowError(modelData)
                        HoverHandler { id: errorHover }
                    }
                }
            }

            // 空列表不该只是一个空框：把「下一步点哪儿」放在这里。
            ColumnLayout {
                anchors.centerIn: parent
                width: parent.width - 32
                spacing: 6
                visible: panel.servers.length === 0
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    color: panel.t.muted
                    font.pixelSize: 11
                    text: panel.loadError.length > 0
                        ? "配置文件读不出来，先按上面的错误改好它。"
                        : "还没有配置 MCP 服务器。点「新增」填一条 stdio 启动命令，"
                            + "它的工具就会出现在模型工具表里。"
                }
            }
        }
    }

    Label {
        objectName: "mcpMessage"
        Layout.fillWidth: true
        visible: text.length > 0
        text: panel.message
        color: panel.messageIsError ? panel.t.bad : panel.t.muted
        font.pixelSize: 11
        wrapMode: Text.Wrap
        maximumLineCount: 8
        elide: Text.ElideRight
    }

    // -- 编辑器 ---------------------------------------------------------------
    // 字段跟着类型显示：stdio 要命令/参数/环境变量/工作目录，http 只要 URL。
    Dialog {
        id: editor
        objectName: "mcpServerEditor"
        modal: true
        anchors.centerIn: parent
        width: Math.min(560, panel.width - 20)
        height: Math.min(620, panel.height - 20)
        padding: 16
        closePolicy: Popup.CloseOnEscape
        title: panel.editing >= 0 ? "修改 MCP 服务器" : "新增 MCP 服务器"

        background: Rectangle {
            color: panel.t.card
            border.color: panel.t.line
            radius: panel.t.radius
        }

        contentItem: ColumnLayout {
            spacing: 10

            ScrollView {
                id: formScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true

                GridLayout {
                    width: formScroll.availableWidth
                    columns: 2
                    columnSpacing: 10
                    rowSpacing: 8

                    Label { text: "名称"; color: panel.t.muted; font.pixelSize: 12 }
                    TextField {
                        objectName: "mcpServerName"
                        Layout.fillWidth: true
                        text: panel.draftName
                        placeholderText: "唯一的名字，工具名前缀取自它"
                        onTextEdited: panel.draftName = text
                    }

                    Label { text: "类型"; color: panel.t.muted; font.pixelSize: 12 }
                    ComboBox {
                        objectName: "mcpServerTransport"
                        Layout.fillWidth: true
                        model: panel.transportOptions
                        currentIndex: panel.draftTransport === "http" ? 1 : 0
                        onActivated: panel.draftTransport = panel.transportOptions[index]
                    }

                    Label { text: "启用"; color: panel.t.muted; font.pixelSize: 12 }
                    CheckBox {
                        objectName: "mcpServerEnabled"
                        text: "启用的服务器才会去连接"
                        checked: panel.draftEnabled
                        onToggled: panel.draftEnabled = checked
                    }

                    Label { visible: panel.draftIsStdio; text: "命令"; color: panel.t.muted; font.pixelSize: 12 }
                    TextField {
                        objectName: "mcpServerCommand"
                        Layout.fillWidth: true
                        visible: panel.draftIsStdio
                        text: panel.draftCommand
                        placeholderText: "例如 python 或 npx（Windows 上建议给完整路径）"
                        onTextEdited: panel.draftCommand = text
                    }

                    Label { visible: panel.draftIsStdio; text: "参数"; color: panel.t.muted; font.pixelSize: 12 }
                    TextField {
                        objectName: "mcpServerArgs"
                        Layout.fillWidth: true
                        visible: panel.draftIsStdio
                        text: panel.draftArgs
                        placeholderText: "空格分隔，例如 server.py --stdio"
                        onTextEdited: panel.draftArgs = text
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.columnSpan: 2
                        visible: panel.draftIsStdio
                        wrapMode: Text.Wrap
                        color: panel.t.muted
                        font.pixelSize: 10
                        text: "参数按空白切开；带空格的一段用一对双引号护住。"
                            + "不做引号嵌套、单引号和反斜杠转义——需要这些就用配置文件。"
                    }

                    Label {
                        visible: panel.draftIsStdio
                        text: "环境变量"
                        color: panel.t.muted
                        font.pixelSize: 12
                        Layout.alignment: Qt.AlignTop
                    }
                    ScrollView {
                        id: envScroll
                        Layout.fillWidth: true
                        Layout.preferredHeight: 78
                        visible: panel.draftIsStdio
                        clip: true

                        TextArea {
                            objectName: "mcpServerEnv"
                            // ScrollView 不会替内容定尺寸：不自己撑满的话，多行输入框
                            // 只有一行高，剩下的空间是一块空白。
                            width: envScroll.availableWidth
                            height: Math.max(implicitHeight, envScroll.availableHeight)
                            // 自己画底色与边框：ScrollView 里的输入框不再有 Fusion 的默认框，
                            // 不画的话这块地方看起来就是一片空白，没人知道能点进去打字。
                            background: Rectangle {
                                color: panel.t.paper
                                border.color: panel.t.line
                                radius: 4
                            }
                            selectByMouse: true
                            wrapMode: TextEdit.NoWrap
                            font.pixelSize: 12
                            text: panel.draftEnv
                            placeholderText: "K=V，每行一条"
                            onTextChanged: panel.draftEnv = text
                        }
                    }

                    Label { visible: panel.draftIsStdio; text: "工作目录"; color: panel.t.muted; font.pixelSize: 12 }
                    TextField {
                        objectName: "mcpServerCwd"
                        Layout.fillWidth: true
                        visible: panel.draftIsStdio
                        text: panel.draftCwd
                        placeholderText: "留空用当前工作目录"
                        onTextEdited: panel.draftCwd = text
                    }

                    Label { visible: !panel.draftIsStdio; text: "URL"; color: panel.t.muted; font.pixelSize: 12 }
                    TextField {
                        objectName: "mcpServerUrl"
                        Layout.fillWidth: true
                        visible: !panel.draftIsStdio
                        text: panel.draftUrl
                        placeholderText: "https://…"
                        onTextEdited: panel.draftUrl = text
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.columnSpan: 2
                        visible: !panel.draftIsStdio
                        wrapMode: Text.Wrap
                        color: panel.t.warn
                        font.pixelSize: 10
                        text: "HTTP 走 MCP 的 Streamable HTTP：填完整地址（例如 http://127.0.0.1:8765/mcp），测试连接与刷新都会真的连一次。"
                    }

                    Label { text: "超时（秒）"; color: panel.t.muted; font.pixelSize: 12 }
                    TextField {
                        objectName: "mcpServerTimeout"
                        Layout.fillWidth: true
                        text: panel.draftTimeout
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        placeholderText: "20"
                        onTextEdited: panel.draftTimeout = text
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    color: panel.t.muted
                    font.pixelSize: 10
                    text: "保存后立刻在后台连接一次，发现到的工具不再需要重启应用。"
                }
                Button {
                    objectName: "mcpServerCancel"
                    text: "取消"
                    flat: true
                    onClicked: editor.close()
                }
                Button {
                    objectName: "mcpServerSave"
                    text: "保存"
                    highlighted: true
                    enabled: panel.draftName.trim().length > 0
                    onClicked: panel.commitEditor()
                }
            }
        }
    }
}
