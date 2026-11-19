import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 独立设置页：一页里两层设置——
//   全局：外观 / 主框构 / 模型渠道 / MCP 服务器 / 技能（这台机器上的默认值）
//   本工程：工程设置（提取粒度、模型、提示词与密钥）/ 关于
// 启动页此前右半边就是这一整块设置区；拆出来后启动页只剩工程列表，
// 两处界面各自变简单：找工程的人不被设置占掉一半宽度，改设置的人不用先看工程。
//
// 「工程设置」不再是工作区里的一页：它整页嵌进这里（embedded），
// 于是工程内只剩导航底部那一个「设置」入口。
//
// 从启动页或工作区都能进来，回哪一页由 Main.qml 记住（openSettings/closeSettings），
// 这一页只负责把「返回」交回去，不自己猜目标。
Item {
    id: page
    objectName: "settingsPage"
    property var app
    readonly property var t: app.theme

    // -- 模型渠道（全局 harness 默认值）的状态 ----------------------------
    // 这些字段原来住在 LauncherPage 里，行为一字未改地搬过来：填表 → 保存 → bridge
    // 落盘，结果仍以 "global_settings" 回到下面那个 Connections。
    property var catalog: []
    property var profile: ({})
    property string settingsPath: ""
    property bool loaded: false
    // MCP 配置文件路径只用来在「关于」里露一行，不参与任何写入。
    property string mcpConfigPath: ""

    readonly property var effortOptions: ["不设置", "off", "minimal", "low", "medium", "high"]

    // 外观三档：跟随系统（默认）、固定浅色、固定深色。选择由 bridge 记住。
    readonly property var appearanceModes: [
        {key: "system", label: "跟随系统"},
        {key: "light", label: "浅色"},
        {key: "dark", label: "深色"}
    ]

    function appearanceIndex(mode) {
        for (var i = 0; i < appearanceModes.length; ++i)
            if (appearanceModes[i].key === mode) return i
        return 0
    }

    // 主框构两档：紧凑（默认，各区域共边）与岛屿（各区域浮成独立卡片，卡片间留缝）。
    readonly property var layoutModes: [
        {key: "compact", label: "紧凑"},
        {key: "islands", label: "岛屿"}
    ]

    function layoutIndex(layout) {
        for (var i = 0; i < layoutModes.length; ++i)
            if (layoutModes[i].key === layout) return i
        return 0
    }

    // -- 分组导航 --------------------------------------------------------
    // 两组分开声明：上面是这台机器的全局默认值，下面是当前工程的设置。
    // sections 是两组按顺序拼起来的扁平清单，它的顺序必须与右侧 StackLayout 的
    // 声明顺序一一对应——错位会让「点外观、改模型」这种静默错误重新出现，
    // 所以顺序只有这一处真相，导航与内容都从它派生。
    readonly property var globalSections: [
        {key: "appearance", label: "外观"},
        {key: "model", label: "模型渠道"},
        {key: "mcp", label: "MCP 服务器"},
        {key: "skill", label: "技能"}
    ]
    readonly property var projectSections: [
        // 没有打开工程时这一节点不可点：它读写的都是当前工程的配置，空工程态点进去
        // 只会报一串「请先打开图项目」。所以这一项自带一句停用原因（别的分组没有）：
        // 说明为什么点不了，而不是只把字染灰。
        {key: "project_settings", label: "工程设置", disabledHint: "先打开工程"},
        {key: "about", label: "关于"}
    ]
    readonly property var sections: globalSections.concat(projectSections)

    // 导航列表按「分组标题 + 该组各项」摊平后交给一个 Repeater：标题行不可点，
    // 两种行只差高度与是否可点，不必为两组各写一份 delegate。
    readonly property var navEntries: {
        var entries = []
        var groups = [
            {group: "global", label: "全局", items: globalSections},
            {group: "project", label: "本工程", items: projectSections}
        ]
        for (var g = 0; g < groups.length; ++g) {
            entries.push({group: groups[g].group, label: groups[g].label})
            for (var i = 0; i < groups[g].items.length; ++i)
                entries.push({section: groups[g].items[i]})
        }
        return entries
    }

    property int sectionIndex: 0

    // 工程设置只对当前打开的工程有意义；没有工程时这一节点禁用而不是藏起来——
    // 藏起来使用者会以为设置页里根本没有这一项。
    readonly property bool projectSettingsAvailable: !!(app.project && app.project.path)

    function sectionAvailable(key) {
        return key !== "project_settings" || projectSettingsAvailable
    }

    function indexOfSection(key) {
        for (var i = 0; i < sections.length; ++i)
            if (sections[i].key === key) return i
        return -1
    }

    function selectSection(key) {
        var index = indexOfSection(key)
        if (index < 0 || !sectionAvailable(key)) return
        sectionIndex = index
    }

    // 打开来源决定默认分组：从工作区点进来（导航底部那个「设置」）通常是要改当前工程的
    // 粒度或模型，先落在「工程设置」；从启动页进来还没有工程，只能落在全局的「外观」。
    function defaultSectionKey() {
        return app.modeBeforeSettings === "workspace" && projectSettingsAvailable
            ? "project_settings" : "appearance"
    }

    // -- 模型渠道：载入 / 表单 ⇄ 值 --------------------------------------
    function load() {
        bridge.loadGlobalSettings()
        // 配置文件路径：MCP 面板自己也会读，这里只取一份路径给「关于」显示。
        var snapshot = bridge.mcpServers()
        page.mcpConfigPath = snapshot.path || ""
    }

    function enter() {
        page.load()
        page.sectionIndex = page.indexOfSection(page.defaultSectionKey())
    }

    // 这一页现在由 Main.qml 的 Loader 在第一次进设置时创建，创建时它的 visible 已是
    // 真值，visibleChanged 不会补发——首次进入在这里走一遍同样的流程。
    // 不按 visible 判断：这一页只会在「正在进设置」的那一次被创建，而创建那一刻
    // 外层 StackLayout 有没有把这格标成可见，取决于属性绑定与 onModeChanged 的先后，
    // 靠它判断会时灵时不灵（实测同一个入口两次运行结果不同）。
    // 之后再进来（页面留着不重建）由 Main.qml 的 onModeChanged 调 enter()：
    // 页面挂在 Loader 下面以后，StackLayout 不再改它的 visible，靠 visibleChanged
    // 已经收不到「又进了一次设置」这件事了。
    Component.onCompleted: page.enter()

    function applyProfile(p) {
        profile = p || ({})
        loaded = true
        var idx = 0
        for (var i = 0; i < catalog.length; ++i)
            if (catalog[i].id === profile.provider) idx = i
        providerBox.currentIndex = idx
        baseUrlField.text = profile.base_url || ""
        baseUrlField.cursorPosition = 0
        modelField.text = profile.model || ""
        keyEnvField.text = profile.key_env || ""
        tokenParamBox.currentIndex = (profile.token_parameter === "max_completion_tokens") ? 1 : 0
        var effort = profile.reasoning_effort || ""
        effortBox.currentIndex = effort === "" ? 0
            : effort === "off" ? 1 : effort === "minimal" ? 2 : effort === "low" ? 3
            : effort === "medium" ? 4 : effort === "high" ? 5 : 0
        maxTokensField.text = profile.max_tokens || ""
        contextField.text = profile.context_window || ""
        outputReserveField.text = profile.output_reserve || ""
        compactionReserveField.text = profile.compaction_reserve || ""
        compactionMaxField.text = profile.compaction_max_tokens || ""
        timeoutField.text = profile.timeout_seconds || ""
        temperatureField.text = profile.temperature === null
            || profile.temperature === undefined ? "" : profile.temperature
    }

    function applyProviderPreset(index) {
        if (index < 0 || index >= catalog.length) return
        var preset = catalog[index]
        baseUrlField.text = preset.base_url || ""
        modelField.text = preset.model || ""
        keyEnvField.text = preset.key_env || ""
        tokenParamBox.currentIndex = (preset.token_parameter === "max_completion_tokens") ? 1 : 0
        var effort = preset.reasoning_effort || ""
        effortBox.currentIndex = effort === "" ? 0
            : effort === "off" ? 1 : effort === "minimal" ? 2 : effort === "low" ? 3
            : effort === "medium" ? 4 : effort === "high" ? 5 : 0
    }

    function collect() {
        return {
            provider: catalog.length > 0 ? catalog[providerBox.currentIndex].id : (profile.provider || "ark"),
            base_url: baseUrlField.text.trim(),
            model: modelField.text.trim(),
            key_env: keyEnvField.text.trim(),
            token_parameter: tokenParamBox.currentIndex === 1 ? "max_completion_tokens" : "max_tokens",
            reasoning_effort: effortBox.currentIndex === 0 ? null : effortOptions[effortBox.currentIndex],
            max_tokens: parseInt(maxTokensField.text, 10) || 0,
            context_window: parseInt(contextField.text, 10) || 0,
            output_reserve: parseInt(outputReserveField.text, 10) || 0,
            compaction_reserve: parseInt(compactionReserveField.text, 10) || 0,
            compaction_max_tokens: parseInt(compactionMaxField.text, 10) || 0,
            timeout_seconds: parseInt(timeoutField.text, 10) || 0,
            // 留空 = 用供应商默认；提取建议 0.2，低了同一段原文跑两遍才稳。
            temperature: temperatureField.text.trim() === ""
                ? null : parseFloat(temperatureField.text)
        }
    }

    function save() {
        bridge.saveGlobalSettings(page.collect())
    }

    Connections {
        target: bridge
        function onResultReady(kind, payload) {
            // 设置区搬走后，结果只该由这一页接：启动页不再监听 global_settings。
            if (kind !== "global_settings") return
            catalog = payload.catalog || []
            settingsPath = payload.path || ""
            page.applyProfile(payload.profile)
            if (payload.notice) app.toast(payload.notice, false)
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // -- 页头 --------------------------------------------------------
        // 高度 = 44 拖动带 + 52 标题行：窗口没有系统标题栏，顶部这 44px 由 Main.qml 里
        // 的浮层接住拖动与窗口按钮；标题行必须从它下面开始，否则「返回」的点击
        // 会被那层拖动区吃掉（那块 MouseArea 在浮层里，压在页面之上）。
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 96
            color: t.card
            border.color: t.line

            RowLayout {
                anchors.fill: parent
                anchors.topMargin: 44
                anchors.leftMargin: 20
                anchors.rightMargin: 20
                spacing: 12

                Button {
                    objectName: "settingsBackButton"
                    text: "← 返回"
                    flat: true
                    // 回哪一页由 Main 决定：从启动页进来的回启动页，从工作区进来的回工作区。
                    onClicked: page.app.closeSettings()
                }
                Label {
                    text: "设置"
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    color: t.ink
                }
                Item { Layout.fillWidth: true }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 20
            spacing: 16

            // -- 分组导航 -------------------------------------------------
            Rectangle {
                Layout.preferredWidth: 200
                Layout.fillHeight: true
                color: t.card
                border.color: t.line
                radius: t.radius

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 4

                    Repeater {
                        // 模型是摊平后的 navEntries：分组标题行也走同一个 delegate，
                        // 靠 modelData.section 是否存在区分标题与可点的分组。
                        model: page.navEntries
                        delegate: Item {
                            id: navEntry
                            required property var modelData
                            readonly property var section: modelData.section === undefined
                                ? null : modelData.section
                            readonly property bool available: navEntry.section
                                ? page.sectionAvailable(navEntry.section.key) : false
                            // 只有「不可用」才留一行原因；可用时不写功能说明（分组名已经够）。
                            readonly property string disabledNote: navEntry.section && !navEntry.available
                                ? (navEntry.section.disabledHint || "") : ""

                            objectName: navEntry.section
                                ? "settingsSectionNav_" + navEntry.section.key
                                : "settingsGroupRow_" + modelData.group
                            Layout.fillWidth: true
                            Layout.preferredHeight: navEntry.section
                                ? (navEntry.disabledNote.length > 0 ? 44 : 30) : 28
                            // 第二组前面留一段距离：两组是一层区分，不能连成一片。
                            Layout.topMargin: navEntry.section ? 0
                                : (modelData.group === "project" ? 10 : 2)

                            Label {
                                objectName: navEntry.section ? "" : "settingsGroupLabel_" + modelData.group
                                visible: !navEntry.section
                                anchors.left: parent.left
                                anchors.leftMargin: 12
                                anchors.verticalCenter: parent.verticalCenter
                                // 标题行才有文字；分组项那一行这个 Label 是隐藏的，
                                // 所以只读有 label 的一侧，别让 undefined 落到 text 上。
                                text: navEntry.section ? "" : (navEntry.modelData.label || "")
                                color: page.t.muted
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                            }

                            Rectangle {
                                visible: !!navEntry.section
                                anchors.fill: parent
                                radius: 6
                                color: navEntry.section && page.sectionIndex === page.indexOfSection(navEntry.section.key)
                                    ? page.t.accentSoft : "transparent"
                            }
                            Column {
                                visible: !!navEntry.section
                                anchors.left: parent.left
                                anchors.leftMargin: 12
                                anchors.right: parent.right
                                anchors.rightMargin: 8
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: 1
                                Label {
                                    text: navEntry.section ? navEntry.section.label : ""
                                    color: !navEntry.available ? page.t.muted
                                        : page.sectionIndex === page.indexOfSection(navEntry.section.key)
                                            ? page.t.accent : page.t.ink
                                    font.pixelSize: 14
                                    font.weight: navEntry.available && page.sectionIndex === page.indexOfSection(navEntry.section.key)
                                        ? Font.DemiBold : Font.Normal
                                }
                                Label {
                                    objectName: navEntry.section ? "settingsSectionHint_" + navEntry.section.key : ""
                                    visible: navEntry.disabledNote.length > 0
                                    text: navEntry.disabledNote
                                    color: page.t.muted
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                    width: parent.width
                                }
                            }
                            MouseArea {
                                objectName: navEntry.section ? "settingsSectionHit_" + navEntry.section.key : ""
                                anchors.fill: parent
                                // 无工程时「工程设置」不可点：禁掉命中区而不是只把字染灰，
                                // 否则点了没反应又切不过去，看起来像界面卡住。
                                enabled: navEntry.available
                                cursorShape: navEntry.available ? Qt.PointingHandCursor : Qt.ArrowCursor
                                onClicked: page.sectionIndex = page.indexOfSection(navEntry.section.key)
                            }
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            // -- 内容区 ---------------------------------------------------
            // 各块内容高度差很大（外观两行 / 模型渠道 14 行表单 / MCP 与技能各一整份面板），
            // 所以按分组分页：切到哪一块，哪一块就拿到全部高度，谁也不压扁谁。
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: t.card
                border.color: t.line
                radius: t.radius

                StackLayout {
                    objectName: "settingsContent"
                    anchors.fill: parent
                    anchors.margins: 16
                    currentIndex: page.sectionIndex

                    // -- 外观（原样搬自启动页）---------------------------------
                    ColumnLayout {
                        objectName: "settingsAppearanceSection"
                        spacing: 10

                        // 字号与启动页里那一份一致（13），搬控件不改观感。
                        Label {
                            text: "外观"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            color: t.ink
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Label {
                                text: "界面深浅色"
                                color: t.muted
                                font.pixelSize: 11
                            }
                            Item { Layout.fillWidth: true }
                            ComboBox {
                                objectName: "appearanceMode"
                                Layout.preferredWidth: 140
                                model: page.appearanceModes
                                textRole: "label"
                                currentIndex: page.appearanceIndex(bridge.themeMode)
                                // 立刻生效并记住选择；不需要按「保存全局设置」。
                                onActivated: bridge.setThemeMode(page.appearanceModes[index].key)
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Label {
                                text: "主框构"
                                color: t.muted
                                font.pixelSize: 11
                            }
                            Item { Layout.fillWidth: true }
                            ComboBox {
                                objectName: "layoutMode"
                                Layout.preferredWidth: 140
                                model: page.layoutModes
                                textRole: "label"
                                currentIndex: page.layoutIndex(bridge.layoutMode)
                                // 立刻生效并记住选择；不需要按「保存全局设置」。
                                onActivated: bridge.setLayoutMode(page.layoutModes[index].key)
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "紧凑：各区域共边，占满窗口（默认）。岛屿：导航、页面、协作者与状态栏各成一张圆角卡片，卡片之间留缝。"
                            color: t.muted
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }
                        Item { Layout.fillHeight: true }
                    }

                    // -- 模型渠道：所有工程共用的 harness 默认值 ------------------
                    // 这一段是从 LauncherPage 整体搬过来的：字段、顺序、保存逻辑都没动，
                    // 只把 page 的归属换成了设置页自己。
                    ColumnLayout {
                        objectName: "settingsModelSection"
                        spacing: 8

                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: "全局设置 · Harness"
                                font.pixelSize: 14
                                font.weight: Font.DemiBold
                                color: t.ink
                            }
                            Item { Layout.fillWidth: true }
                            Button {
                                text: "重新载入"
                                flat: true
                                onClicked: bridge.loadGlobalSettings()
                            }
                        }
                        ScrollView {
                            id: settingsScroll
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true

                            GridLayout {
                                width: settingsScroll.availableWidth
                                columns: 2
                                columnSpacing: 10
                                rowSpacing: 8

                                Label { text: "服务"; color: t.muted; font.pixelSize: 12 }
                                ComboBox {
                                    id: providerBox
                                    Layout.fillWidth: true
                                    model: page.catalog.map(function (item) { return item.name })
                                    onActivated: page.applyProviderPreset(index)
                                }

                                Label { text: "地址"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: baseUrlField
                                    Layout.fillWidth: true
                                    placeholderText: "https://…"
                                }

                                Label { text: "模型"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: modelField
                                    Layout.fillWidth: true
                                    placeholderText: "模型 ID"
                                }

                                Label { text: "推理强度"; color: t.muted; font.pixelSize: 12 }
                                ComboBox {
                                    id: effortBox
                                    Layout.fillWidth: true
                                    model: page.effortOptions
                                }

                                Label { text: "密钥变量"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: keyEnvField
                                    Layout.fillWidth: true
                                    placeholderText: "ARK_API_KEY"
                                }

                                Label { text: "输出参数名"; color: t.muted; font.pixelSize: 12 }
                                ComboBox {
                                    id: tokenParamBox
                                    Layout.fillWidth: true
                                    model: ["max_tokens", "max_completion_tokens"]
                                }

                                Label { text: "最大输出"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: maxTokensField
                                    Layout.fillWidth: true
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }

                                Label { text: "上下文窗口"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: contextField
                                    Layout.fillWidth: true
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }

                                Label { text: "输出预留"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: outputReserveField
                                    Layout.fillWidth: true
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }

                                Label { text: "压缩预留"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: compactionReserveField
                                    Layout.fillWidth: true
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }

                                Label { text: "压缩最大输出"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: compactionMaxField
                                    Layout.fillWidth: true
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }

                                Label { text: "响应空闲超时（秒）"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: timeoutField
                                    Layout.fillWidth: true
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }

                                Label { text: "温度"; color: t.muted; font.pixelSize: 12 }
                                TextField {
                                    id: temperatureField
                                    Layout.fillWidth: true
                                    placeholderText: "留空用供应商默认；提取建议 0.2"
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                }
                            }
                        }

                        Label {
                            objectName: "settingsPathLabel"
                            Layout.fillWidth: true
                            text: page.settingsPath
                            color: t.muted
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
                        }
                        Button {
                            objectName: "saveGlobalSettingsButton"
                            Layout.fillWidth: true
                            text: "保存全局设置"
                            highlighted: true
                            enabled: page.loaded && !app.busy
                            onClicked: page.save()
                        }
                    }

                    // -- MCP 服务器：外部工具服务器，跨工程共用 ------------------
                    // 面板本身不动（McpSettingsPanel 自带列表、编辑器与刷新），这里只加
                    // 一层容器：分组标识要留在这个页面上，测试与截图都靠它定位。
                    Item {
                        objectName: "settingsMcpSection"

                        McpSettingsPanel {
                            anchors.fill: parent
                            app: page.app
                        }
                    }

                    // -- 技能：可保存的「作业说明 + 工具组」，跨工程共用 --------------
                    // 同 MCP 一处一样：面板自己管列表、编辑器与刷新，这里只加一层容器，
                    // 分组标识留在设置页上，测试与截图都靠它定位。
                    Item {
                        objectName: "settingsSkillSection"

                        SkillSettingsPanel {
                            anchors.fill: parent
                            app: page.app
                        }
                    }

                    // -- 本工程 · 工程设置：整页嵌进 ProjectSettingsPage ----------------
                    // 不复制它的控件：这一节只是把那一页放进来，字段、密钥校验与保存
                    // 逻辑都还是 ProjectSettingsPage 里那一份。embedded 让它收起自己
                    // 的页头（标题 + 窗口按钮），在这里就是设置页里的一节内容。
                    // 容器留着 objectName：测试与截图靠它定位「本工程」这一组。
                    Item {
                        objectName: "settingsProjectSection"

                        ProjectSettingsPage {
                            anchors.fill: parent
                            app: page.app
                            embedded: true
                        }
                    }

                    // -- 关于 -----------------------------------------------------
                    ColumnLayout {
                        objectName: "settingsAboutSection"
                        spacing: 8

                        Label {
                            text: "关于"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            color: t.ink
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Label { text: "版本"; color: t.muted; font.pixelSize: 12 }
                            Item { Layout.fillWidth: true }
                            Label {
                                objectName: "settingsVersionLabel"
                                text: bridge.appVersion
                                color: t.ink
                                font.pixelSize: 12
                            }
                        }
                        Label { text: "模型渠道配置文件"; color: t.muted; font.pixelSize: 12 }
                        Label {
                            objectName: "settingsFileLabel"
                            Layout.fillWidth: true
                            text: page.settingsPath
                            color: t.ink
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
                        }
                        Label { text: "MCP 服务器配置文件"; color: t.muted; font.pixelSize: 12 }
                        Label {
                            objectName: "settingsMcpFileLabel"
                            Layout.fillWidth: true
                            text: page.mcpConfigPath
                            color: t.ink
                            font.pixelSize: 10
                            elide: Text.ElideMiddle
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
        }
    }
}
