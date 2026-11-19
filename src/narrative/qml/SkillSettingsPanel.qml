import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 全局设置里的「技能」：可保存的「一段作业说明 + 要打开的工具组」。
//
// 与 MCP 面板同一分工：列表永远来自 bridge.skills()（磁盘上的真值），编辑器里那份是
// 可丢弃的草稿，按「保存」才整份写回。手改了 skills.json、或者别处改过配置，重开面板
// 看到的就是磁盘上的样子。
//
// 内置技能（builtin=true）来自代码、随程序发布：编辑/删除/启用开关在这里就置灰，不让
// 用户白点一次再被内核拒绝；内置标记也直接来自返回，界面不按名字猜哪几条只读。
ColumnLayout {
    id: panel
    objectName: "skillSettingsPanel"

    property var app
    readonly property var t: app ? app.theme : bridge.theme

    property var skills: []
    // 内置提示词位（来自 bridge.skills() 的 prompt_keys）：key/label/description/version/
    // replaced_by/source。它们随程序发布、只读，面板只负责列出来并给一个「复制成我的技能」
    // 的入口——想改措辞的做法是复制一份再改，而不是就地改内置文本。
    property var promptKeys: []
    // 技能文件读不出来时提示词位全部回落到内置：原因显示一行，不整块藏起来。
    property string promptProblem: ""
    // 编辑器的可选项：业务组名与工具名都是注册表的静态事实，不受现有技能影响。
    property var families: []
    property var tools: []
    property string configPath: ""
    property string loadError: ""
    property string startupError: ""
    property string message: ""
    property bool messageIsError: false
    // 一次保存/删除在跑：写盘是毫秒级，但按钮仍按状态停一下，免得连点两次用同一份
    // 草稿写两遍。技能不建连、不起子进程，所以这里没有后台线程（MCP 那组才有）。
    property bool busy: false
    // 保存失败过：编辑器重新打开时把草稿原样还给用户，别让他重敲一遍。
    property bool keepDraft: false
    property int editing: -1

    // 编辑草稿。都是独立属性而不是一个对象：QML 只对「整个 var 属性被替换」发通知，
    // 改对象里的字段不会刷新绑定。
    // draftOriginal 是「要改谁」：为空表示新建，非空是已保存技能的名字——名字是身份，
    // 内核按名字定位（没有改名接口），所以编辑已有技能时名称框只读。
    property string draftOriginal: ""
    property string draftName: ""
    property string draftSummary: ""
    property string draftInstructions: ""
    property bool draftEnabled: true
    property var draftFamilies: []
    property var draftTools: []
    // 这条技能的正文顶替哪段内置提示词（键名）；空串 = 只是附加说明。
    // 从「内置提示词」区复制出来的草稿默认就指回那个键——不顶替任何提示词的副本
    // 只会是一段没人加载的说明，复制这个动作就白做了。
    property string draftReplaces: ""
    // 额外工具的搜索词：可选工具有一百多个，全铺开没人找得到。
    property string toolQuery: ""
    // 「会打开哪些工具」预览：由桥解析（解析口径只写一份），界面不自己算。
    property var previewTools: []
    property string previewProblem: ""

    spacing: 8

    Component.onCompleted: panel.reload()
    // 页面常驻在 StackLayout 里：只有真正露出来才去读文件，每次进来也顺手刷新一遍，
    // 免得显示的是上次进来时的旧值（手改过配置文件时尤其明显）。
    onVisibleChanged: if (visible) panel.reload()

    onDraftFamiliesChanged: panel.refreshPreview()
    onDraftToolsChanged: panel.refreshPreview()

    function reload() {
        var snapshot = bridge.skills()
        panel.skills = snapshot.skills || []
        panel.promptKeys = snapshot.prompt_keys || []
        panel.promptProblem = snapshot.prompt_problem || ""
        panel.families = snapshot.families || []
        panel.tools = snapshot.tools || []
        panel.configPath = snapshot.path || ""
        panel.loadError = snapshot.error || ""
        panel.startupError = snapshot.startup_error || ""
    }

    // -- 列表上的说明文字 -----------------------------------------------------

    function opensText(skill) {
        var opens = skill.opens || ({})
        if (opens.problem) return "工具解析失败"
        var names = opens.resolved_tools || []
        return names.length > 0 ? "会打开 " + names.length + " 个工具" : "不打开工具"
    }

    function opensTip(skill) {
        var opens = skill.opens || ({})
        if (opens.problem) return opens.problem
        var names = opens.resolved_tools || []
        if (names.length === 0) return "这条技能只给说明，不打开任何工具。"
        return names.join("\n")
    }

    function groupsText(skill) {
        var parts = []
        var families = skill.families || []
        var tools = skill.tools || []
        if (families.length > 0) parts.push("业务组：" + families.join("、"))
        if (tools.length > 0) parts.push("额外工具：" + tools.join("、"))
        // 顶替提示词与生成工作流都是「这条技能会改什么」：列表一行说清楚，
        // 否则只能打开编辑器才知道它是不是替换了系统提示词。
        if (skill.replaces) parts.push("替换提示词：" + skill.replaces)
        var workflow = skill.workflow
        if (workflow && workflow.kind)
            parts.push("工作流：" + workflow.kind + "／" + (workflow.model || "未声明模型"))
        return parts.length > 0 ? parts.join("；") : "不点名任何业务组或工具"
    }

    // 提示词位的当前状态：谁覆盖了它，或者内置仍是生效的那一份。
    function promptStatus(row) {
        if (row && row.replaced_by) return "被「" + row.replaced_by + "」覆盖"
        return "内置生效（版本 " + ((row && row.version) || "1") + "）"
    }

    // 内置技能的提示气泡非读不可：按钮置灰之后，用户更需要知道「为什么不能改」。
    function rowEditTip(skill) {
        return skill && skill.builtin === true ? "内置技能随程序发布，不能改删" : "修改这条技能"
    }

    function rowDeleteTip(skill) {
        return skill && skill.builtin === true ? "内置技能随程序发布，不能改删" : "删除这条技能"
    }

    function rowEnabledTip(skill) {
        return skill && skill.builtin === true
            ? "内置技能随程序发布，不能改删"
            : "停用的技能不会被 load_skill 加载"
    }

    // -- 编辑器 ---------------------------------------------------------------

    function newSkill() {
        panel.editing = -1
        panel.draftOriginal = ""
        panel.draftName = ""
        panel.draftSummary = ""
        panel.draftInstructions = ""
        panel.draftEnabled = true
        panel.draftFamilies = []
        panel.draftTools = []
        panel.draftReplaces = ""
        panel.toolQuery = ""
        panel.keepDraft = false
        panel.fillFields()
        editor.open()
    }

    // 把一份草稿（新建技能、或从内置提示词复制出来的）灌进编辑器。
    // 只有新建才走这里：已有技能走 openEditor，名称是身份、不能改。
    function applyDraft(draft) {
        draft = draft || ({})
        panel.editing = -1
        panel.draftOriginal = ""
        panel.draftName = draft.name || ""
        panel.draftSummary = draft.summary || ""
        panel.draftInstructions = draft.instructions || ""
        panel.draftEnabled = draft.enabled !== false
        panel.draftFamilies = (draft.families || []).slice()
        panel.draftTools = (draft.tools || []).slice()
        panel.draftReplaces = draft.replaces || ""
        panel.toolQuery = ""
        panel.keepDraft = false
        panel.fillFields()
        editor.open()
    }

    // 「复制成我的技能」：草稿由桥生成（当前生效全文 + 默认 replaces），这里不落盘——
    // 用户改完点保存才写进技能文件。
    function copyPromptAsSkill(key) {
        var result = bridge.duplicatePromptAsSkill(key)
        // 未知键之类的问题已由桥发 errorOccurred，面板那条消息会显示原因；这里不再开编辑器。
        if (!result || result.ok !== true) return
        panel.applyDraft(result.draft)
        panel.messageIsError = false
        panel.message = "已复制提示词「" + result.label + "」为草稿（替换 " + result.key
            + "）；改完点保存才写进技能文件。"
    }

    function openEditor(index) {
        var skill = panel.skills[index]
        // 内置技能不打开编辑器：列表里已经置灰，这里再兜一层，防止误把它当可写的。
        if (!skill || skill.builtin === true) return
        panel.editing = index
        panel.draftOriginal = skill.name
        panel.draftName = skill.name
        panel.draftSummary = skill.summary || ""
        panel.draftInstructions = skill.instructions || ""
        panel.draftEnabled = skill.enabled !== false
        panel.draftFamilies = (skill.families || []).slice()
        panel.draftTools = (skill.tools || []).slice()
        panel.draftReplaces = skill.replaces || ""
        panel.toolQuery = ""
        panel.keepDraft = false
        panel.fillFields()
        editor.open()
    }

    // 保存失败时只重开，不重置草稿：用户刚填的内容还在。
    function reopenEditor() {
        panel.keepDraft = false
        editor.open()
    }

    // 输入框的 text 不绑定草稿：用户一打字绑定就断了，换一条技能时会看到上一条的内容。
    // 所以每次打开编辑器显式灌一次值，之后草稿只由输入框回写。
    function fillFields() {
        nameField.text = panel.draftName
        summaryField.text = panel.draftSummary
        instructionsArea.text = panel.draftInstructions
        replacesField.text = panel.draftReplaces
        enabledBox.checked = panel.draftEnabled
        toolQueryField.text = panel.toolQuery
        panel.refreshPreview()
    }

    function refreshPreview() {
        var opens = bridge.skillOpens(panel.draftFamilies, panel.draftTools)
        panel.previewTools = opens.resolved_tools || []
        panel.previewProblem = opens.problem || ""
    }

    function previewText() {
        if (panel.previewProblem.length > 0)
            return "会打开的工具：解析失败——" + panel.previewProblem
        var names = panel.previewTools
        if (names.length === 0) return "不打开任何工具：这条技能只给模型一段说明。"
        var head = names.slice(0, 10).join("、")
        return "会打开 " + names.length + " 个工具：" + head + (names.length > 10 ? " 等" : "")
    }

    // -- 多选 -----------------------------------------------------------------
    // 勾选状态一律从草稿算（isFamilyChecked / isToolChecked），点击只改草稿：
    // 状态不存在控件里，就不会出现「换一条技能后还是上一条的勾」。

    function isFamilyChecked(name) {
        return (panel.draftFamilies || []).indexOf(name) >= 0
    }

    function toggleFamily(name) {
        var rows = (panel.draftFamilies || []).slice()
        var at = rows.indexOf(name)
        if (at >= 0) rows.splice(at, 1)
        else rows.push(name)
        panel.draftFamilies = rows  // 整份替换：QML 只对属性被替换发通知
    }

    function isToolChecked(name) {
        return (panel.draftTools || []).indexOf(name) >= 0
    }

    function toggleTool(name) {
        var rows = (panel.draftTools || []).slice()
        var at = rows.indexOf(name)
        if (at >= 0) rows.splice(at, 1)
        else rows.push(name)
        panel.draftTools = rows
    }

    // 工具多选框列出什么：没搜索时只列已选的（否则一百多条铺满整块），搜索时按名字
    // 子串过滤并封顶——搜索框不是分页器，找不到就再写细一点。
    function toolChoices() {
        var query = panel.toolQuery.trim().toLowerCase()
        if (query.length === 0) return (panel.draftTools || []).slice()
        var pool = panel.tools || []
        var rows = []
        for (var i = 0; i < pool.length; ++i) {
            if (pool[i].toLowerCase().indexOf(query) < 0) continue
            rows.push(pool[i])
            if (rows.length >= 60) break
        }
        return rows
    }

    // -- 提交 -----------------------------------------------------------------

    function draftPayload() {
        return {
            original: panel.draftOriginal,
            name: panel.draftName.trim(),
            summary: panel.draftSummary.trim(),
            instructions: panel.draftInstructions.trim(),
            families: (panel.draftFamilies || []).slice(),
            tools: (panel.draftTools || []).slice(),
            enabled: panel.draftEnabled,
            // 空串 = 明确「不替换任何提示词」（桥会把它传给内核当清空语义）；
            // 不带这个键则表示「没动过」，那时内核保留已有声明。
            replaces: (panel.draftReplaces || "").trim()
        }
    }

    function commitEditor() {
        var payload = panel.draftPayload()
        editor.close()
        panel.submit(payload, true)
    }

    function submit(payload, keepsDraft) {
        panel.busy = true
        panel.keepDraft = keepsDraft
        panel.messageIsError = false
        panel.message = "正在保存技能…"
        bridge.saveSkill(payload)
    }

    function toggleSkill(index, enabled) {
        var skill = panel.skills[index]
        if (!skill || skill.builtin === true) return
        // 整行原样回传，只翻启用位：update_skill 是部分更新，漏传的字段不会被清空，
        // 但摘要/正文这类必填字段传空会被内核拒绝——所以这里照抄磁盘上的值。
        panel.submit({
            original: skill.name,
            name: skill.name,
            summary: skill.summary || "",
            instructions: skill.instructions || "",
            families: (skill.families || []).slice(),
            tools: (skill.tools || []).slice(),
            enabled: enabled
        }, false)
    }

    function removeSkill(index) {
        var skill = panel.skills[index]
        if (!skill || skill.builtin === true) return
        panel.busy = true
        panel.keepDraft = false
        panel.messageIsError = false
        panel.message = "正在删除「" + skill.name + "」…"
        bridge.deleteSkill(skill.name)
    }

    Connections {
        target: bridge
        function onSkillsChanged() {
            panel.reload()
            panel.busy = false
            panel.keepDraft = false
            if (panel.loadError.length > 0) {
                panel.messageIsError = true
                panel.message = panel.loadError
                return
            }
            panel.messageIsError = false
            panel.message = "技能配置已更新：共 " + panel.skills.length + " 条（含内置）"
            if (panel.app && panel.app.toast) panel.app.toast(panel.message, false)
        }
        function onErrorOccurred(kind, text) {
            if (kind !== "skill") return
            panel.busy = false
            panel.messageIsError = true
            panel.message = text
            // 被拒的原因通常是某个字段不合法：把编辑框重新打开，草稿还在。
            if (panel.keepDraft) panel.reopenEditor()
        }
    }

    // -- 头部：标题、新建、配置文件位置 ---------------------------------------

    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Label {
            text: "技能"
            font.pixelSize: 13
            font.weight: Font.DemiBold
            color: panel.t.ink
        }
        Label {
            text: panel.skills.length + " 条"
            color: panel.t.muted
            font.pixelSize: 11
        }
        Item { Layout.fillWidth: true }
        Button {
            objectName: "skillAddSkill"
            text: "新建技能"
            enabled: !panel.busy && panel.loadError.length === 0
            onClicked: panel.newSkill()
        }
        Button {
            objectName: "skillReload"
            text: "重新载入"
            flat: true
            enabled: !panel.busy
            onClicked: panel.reload()
        }
    }

    Label {
        Layout.fillWidth: true
        wrapMode: Text.Wrap
        color: panel.t.muted
        font.pixelSize: 11
        text: "技能是一段给模型看的作业说明，外加它要打开的业务组与工具。它不替换系统提示词，"
            + "只在会话里被 load_skill 显式加载之后生效；能不能真的打开工具仍由那次会话的访问模式决定。"
    }
    Label {
        objectName: "skillConfigPath"
        Layout.fillWidth: true
        text: "配置文件：" + panel.configPath
        color: panel.t.muted
        font.pixelSize: 10
        elide: Text.ElideMiddle
        ToolTip.visible: pathHover.hovered
        ToolTip.text: panel.configPath
        HoverHandler { id: pathHover }
    }
    Label {
        objectName: "skillLoadError"
        Layout.fillWidth: true
        visible: text.length > 0
        text: panel.loadError
        color: panel.t.bad
        font.pixelSize: 11
        wrapMode: Text.Wrap
    }
    Label {
        objectName: "skillStartupError"
        Layout.fillWidth: true
        visible: text.length > 0
        text: "上次启动注册技能工具时失败：" + panel.startupError
        color: panel.t.muted
        font.pixelSize: 10
        wrapMode: Text.Wrap
    }

    // -- 技能列表 -------------------------------------------------------------

    Rectangle {
        Layout.fillWidth: true
        Layout.fillHeight: true
        color: panel.t.card
        border.color: panel.t.line
        radius: panel.t.radius

        ListView {
            id: skillList
            objectName: "skillList"
            anchors.fill: parent
            anchors.margins: 6
            clip: true
            spacing: 4
            model: panel.skills

            delegate: Rectangle {
                required property int index
                required property var modelData

                width: ListView.view.width
                // 行高跟着内容走：有「工具解析失败」说明时多一行，不能挤进固定高度里。
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
                            objectName: "skillRowName"
                            Layout.fillWidth: true
                            text: modelData.name
                            color: panel.t.ink
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Label {
                            objectName: "skillRowBuiltin"
                            visible: modelData.builtin === true
                            text: "内置"
                            color: panel.t.muted
                            font.pixelSize: 10
                            ToolTip.visible: builtinHover.hovered
                            ToolTip.text: "随程序发布，不能改删"
                            HoverHandler { id: builtinHover }
                        }
                        Label {
                            objectName: "skillRowOpens"
                            text: panel.opensText(modelData)
                            color: (modelData.opens && modelData.opens.problem)
                                ? panel.t.bad : panel.t.muted
                            font.pixelSize: 11
                            ToolTip.visible: opensHover.hovered
                            ToolTip.text: panel.opensTip(modelData)
                            HoverHandler { id: opensHover }
                        }
                        Switch {
                            id: enabledSwitch
                            objectName: "skillRowEnabled"
                            enabled: modelData.builtin !== true && !panel.busy
                            ToolTip.visible: enabledHover.hovered
                            ToolTip.text: panel.rowEnabledTip(modelData)
                            HoverHandler { id: enabledHover }
                            // checked 跟着磁盘上的值走：用户点一下之后普通绑定就断了，
                            // 所以用 Binding，保存失败或列表重读后能回到真实状态。
                            Binding {
                                target: enabledSwitch
                                property: "checked"
                                value: modelData.enabled !== false
                                restoreMode: Binding.RestoreNone
                            }
                            // toggled 对「程序改 checked」也会发：只在与磁盘状态不一致时
                            // 才提交，否则重读列表本身就会触发一次多余的保存。
                            onToggled: {
                                if (enabledSwitch.checked !== (modelData.enabled !== false))
                                    panel.toggleSkill(index, enabledSwitch.checked)
                            }
                        }
                        Label {
                            objectName: "skillRowEdit"
                            text: "编辑"
                            color: modelData.builtin === true || panel.busy
                                ? panel.t.muted : panel.t.accent
                            font.pixelSize: 11
                            font.underline: modelData.builtin !== true
                            ToolTip.visible: editHover.hovered
                            ToolTip.text: panel.rowEditTip(modelData)
                            HoverHandler { id: editHover }
                            MouseArea {
                                objectName: "skillRowEditHit"
                                anchors.fill: parent
                                // 只禁用点击，不禁用标签：置灰的标签仍然接得住悬停，
                                // tooltip 才有机会解释「随程序发布，不能改删」。
                                enabled: modelData.builtin !== true && !panel.busy
                                cursorShape: Qt.PointingHandCursor
                                onClicked: panel.openEditor(index)
                            }
                        }
                        Label {
                            objectName: "skillRowDelete"
                            text: "删除"
                            color: modelData.builtin === true || panel.busy
                                ? panel.t.muted : panel.t.bad
                            font.pixelSize: 11
                            font.underline: modelData.builtin !== true
                            ToolTip.visible: deleteHover.hovered
                            ToolTip.text: panel.rowDeleteTip(modelData)
                            HoverHandler { id: deleteHover }
                            MouseArea {
                                objectName: "skillRowDeleteHit"
                                anchors.fill: parent
                                enabled: modelData.builtin !== true && !panel.busy
                                cursorShape: Qt.PointingHandCursor
                                onClicked: panel.removeSkill(index)
                            }
                        }
                    }
                    Label {
                        objectName: "skillRowSummary"
                        Layout.fillWidth: true
                        text: (modelData.summary || "（没有摘要）")
                            + (modelData.enabled === false ? "（已停用）" : "")
                        color: modelData.enabled === false ? panel.t.warn : panel.t.muted
                        font.pixelSize: 11
                        elide: Text.ElideRight
                        maximumLineCount: 1
                    }
                    Label {
                        objectName: "skillRowGroups"
                        Layout.fillWidth: true
                        text: panel.groupsText(modelData)
                        color: panel.t.muted
                        font.pixelSize: 10
                        elide: Text.ElideRight
                        maximumLineCount: 1
                    }
                    Label {
                        objectName: "skillRowProblem"
                        Layout.fillWidth: true
                        visible: !!modelData.opens && (modelData.opens.problem || "").length > 0
                        text: modelData.opens ? (modelData.opens.problem || "") : ""
                        color: panel.t.bad
                        font.pixelSize: 10
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                }
            }

            // 空列表不该只是一个空框：把「下一步点哪儿」放在这里。
            ColumnLayout {
                anchors.centerIn: parent
                width: parent.width - 32
                spacing: 6
                visible: panel.skills.length === 0
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    color: panel.t.muted
                    font.pixelSize: 11
                    text: panel.loadError.length > 0
                        ? "技能文件读不出来，先按上面的错误改好它。"
                        : "还没有可显示的技能。内置技能随程序发布，正常情况下至少六条"
                            + "（三条作业说明 + 三条生成工作流）；一条也没有说明技能文件被清空了，"
                            + "可以在这里新建一条，或把备份 skills.json.bak 放回来。"
                }
            }
        }
    }

    // -- 内置提示词：可见、可复制 ---------------------------------------------
    // 17 条内置提示词随程序发布、只读。想改措辞的做法是复制一份再改：这里列出每一条
    // 现在是谁在生效（内置还是某条技能覆盖），并给一个「复制成我的技能」的入口。
    // 用 ScrollView + Column + Repeater（而不是 ListView）：17 行不多，全部实例化之后
    // 面板与测试都能直接数出「17 条」，不必滚动才看见。
    Rectangle {
        objectName: "skillPromptPanel"
        Layout.fillWidth: true
        Layout.preferredHeight: 196
        color: panel.t.card
        border.color: panel.t.line
        radius: panel.t.radius

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 8
            spacing: 4

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Label {
                    text: "内置提示词"
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    color: panel.t.ink
                }
                Label {
                    objectName: "skillPromptCount"
                    text: panel.promptKeys.length + " 条"
                    color: panel.t.muted
                    font.pixelSize: 11
                }
                Item { Layout.fillWidth: true }
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignRight
                    text: "复制出来的技能可以改；内置原文一个字符都不动"
                    color: panel.t.muted
                    font.pixelSize: 10
                    elide: Text.ElideRight
                }
            }
            Label {
                objectName: "skillPromptHint"
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: panel.t.muted
                font.pixelSize: 10
                text: "「替换提示词」指这条技能的正文顶替哪一段内置提示词。同一个键有多条时，"
                    + "最近更新的那条生效；停用或删除它就自动回落到内置。"
            }
            Label {
                objectName: "skillPromptProblem"
                Layout.fillWidth: true
                visible: text.length > 0
                text: panel.promptProblem
                color: panel.t.bad
                font.pixelSize: 10
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }

            ScrollView {
                id: promptScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                // 提示词名是单列竖排：横向滚动条只会和竖条互相挤。
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                Column {
                    width: promptScroll.availableWidth
                    spacing: 2

                    Repeater {
                        model: panel.promptKeys

                        delegate: Rectangle {
                            id: promptRow
                            required property var modelData

                            objectName: "skillPromptRow"
                            width: promptScroll.availableWidth
                            height: 22
                            radius: 4
                            color: promptHover.hovered ? panel.t.accentSoft : "transparent"

                            HoverHandler { id: promptHover }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 6
                                anchors.rightMargin: 4
                                spacing: 8

                                Label {
                                    objectName: "skillPromptLabel"
                                    text: promptRow.modelData.label
                                    color: panel.t.ink
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                    Layout.maximumWidth: 200
                                }
                                Label {
                                    objectName: "skillPromptKey"
                                    text: promptRow.modelData.key
                                    color: panel.t.muted
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                    Layout.maximumWidth: 140
                                }
                                Label {
                                    // 谁在生效：内置原文，还是某条技能覆盖了它（点名道姓，
                                    // 用户才知道该去改哪一条）。
                                    objectName: "skillPromptStatus"
                                    Layout.fillWidth: true
                                    text: panel.promptStatus(promptRow.modelData)
                                    color: promptRow.modelData.replaced_by ? panel.t.warn : panel.t.muted
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                                Label {
                                    objectName: "skillPromptCopy_" + promptRow.modelData.key
                                    text: "复制成我的技能"
                                    color: panel.t.accent
                                    font.pixelSize: 11
                                    font.underline: true
                                    MouseArea {
                                        objectName: "skillPromptCopyHit"
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: panel.copyPromptAsSkill(promptRow.modelData.key)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Label {
        objectName: "skillMessage"
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
    // 名称（编辑时只读）、摘要、正文、启用、业务组多选、额外工具多选 + 搜索、实时预览。
    Dialog {
        id: editor
        objectName: "skillEditor"
        modal: true
        anchors.centerIn: parent
        width: Math.min(680, panel.width - 20)
        height: Math.min(660, panel.height - 20)
        padding: 16
        closePolicy: Popup.CloseOnEscape
        title: panel.editing >= 0 ? "修改技能" : "新建技能"

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
                // 这是一张竖排表单：横向滚动条在这里只会和竖向滚动条互相挤，
                // 挤到最后两条都常驻（可用宽度在有无竖条之间来回跳）。
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                ColumnLayout {
                    width: formScroll.availableWidth
                    spacing: 8

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: 10
                        rowSpacing: 8

                        Label { text: "名称"; color: panel.t.muted; font.pixelSize: 12 }
                        TextField {
                            id: nameField
                            objectName: "skillName"
                            Layout.fillWidth: true
                            readOnly: panel.editing >= 0
                            placeholderText: "一眼能看出用途的名字，例如「人物一致性检查」"
                            onTextEdited: panel.draftName = text
                        }
                        Label {
                            Layout.fillWidth: true
                            Layout.columnSpan: 2
                            visible: panel.editing >= 0
                            wrapMode: Text.Wrap
                            color: panel.t.muted
                            font.pixelSize: 10
                            text: "名字是这条技能的身份，不能改；要换名字请另存一条。"
                        }

                        Label { text: "摘要"; color: panel.t.muted; font.pixelSize: 12 }
                        TextField {
                            id: summaryField
                            objectName: "skillSummary"
                            Layout.fillWidth: true
                            placeholderText: "一行说明它解决什么问题"
                            onTextEdited: panel.draftSummary = text
                        }

                        Label { text: "启用"; color: panel.t.muted; font.pixelSize: 12 }
                        CheckBox {
                            id: enabledBox
                            objectName: "skillEnabled"
                            text: "启用的技能才能在会话里被加载"
                            onToggled: panel.draftEnabled = enabledBox.checked
                        }

                        Label { text: "替换提示词"; color: panel.t.muted; font.pixelSize: 12 }
                        TextField {
                            id: replacesField
                            objectName: "skillReplaces"
                            Layout.fillWidth: true
                            placeholderText: "提示词条目名，例如 extraction；留空 = 只附加说明"
                            onTextEdited: panel.draftReplaces = text
                        }
                        Label {
                            Layout.fillWidth: true
                            Layout.columnSpan: 2
                            wrapMode: Text.Wrap
                            color: panel.t.muted
                            font.pixelSize: 10
                            text: "填了条目名，这条技能的正文就顶替那一段内置提示词（同键多条时最近更新的生效，"
                                + "停用或删除即回落内置）；留空表示它只是附加说明，不改任何提示词。"
                                + "从「内置提示词」区复制出来的草稿已经填好这一栏。"
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: "正文（给模型看的作业说明）"
                        color: panel.t.muted
                        font.pixelSize: 12
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        color: panel.t.muted
                        font.pixelSize: 10
                        text: "这一整段会进模型上下文：写成「先调用哪个工具、看到什么再往下走、什么不算完成」，"
                            + "而不是写给人看的介绍。技能不替换系统提示词，只在会话里被 load_skill 加载后生效。"
                    }
                    TextArea {
                        id: instructionsArea
                        objectName: "skillInstructions"
                        // 直接放进 ColumnLayout（项目里其它多行输入框都这么写），不再套一层
                        // ScrollView：正文自己带滚动，套两层滚动区会互相抢滚轮，还容易在
                        // 未打开时把占位文字撑成绑定环。
                        Layout.fillWidth: true
                        Layout.preferredHeight: 200
                        clip: true
                        // 自己画底色与边框：不画的话这块地方看起来就是一片空白。
                        background: Rectangle {
                            color: panel.t.paper
                            border.color: panel.t.line
                            radius: 4
                        }
                        padding: 6
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        font.pixelSize: 12
                        placeholderText: "例如：先用 list_nodes 取人物清单，再用 node_properties 逐一核对…"
                        onTextChanged: panel.draftInstructions = text
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Label { text: "业务组"; color: panel.t.muted; font.pixelSize: 12 }
                        Label {
                            Layout.fillWidth: true
                            text: "点一下选中/取消；选中会一并打开这一组的工具"
                            color: panel.t.muted
                            font.pixelSize: 10
                            elide: Text.ElideRight
                        }
                    }
                    ScrollView {
                        id: familyScroll
                        Layout.fillWidth: true
                        // 19 个业务组按宽度换行，四行也放得下：高度不够的话最后一行会被
                        // 裁掉半行，看起来像和下一块叠在一起。
                        Layout.preferredHeight: 120
                        clip: true
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                        Flow {
                            width: familyScroll.availableWidth
                            spacing: 6

                            Repeater {
                                model: panel.families
                                delegate: Rectangle {
                                    id: familyChip
                                    required property var modelData
                                    readonly property bool picked: panel.isFamilyChecked(modelData)

                                    objectName: "skillFamily_" + modelData
                                    implicitWidth: familyChipLabel.implicitWidth + 16
                                    implicitHeight: 22
                                    radius: 11
                                    // 勾选状态从草稿算，不存控件里：换一条技能时不会留着上一条的勾。
                                    color: familyChip.picked ? panel.t.accentSoft : "transparent"
                                    border.color: familyChip.picked ? panel.t.accent : panel.t.line

                                    Label {
                                        id: familyChipLabel
                                        anchors.centerIn: parent
                                        text: familyChip.modelData
                                        color: familyChip.picked ? panel.t.accent : panel.t.muted
                                        font.pixelSize: 11
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: panel.toggleFamily(familyChip.modelData)
                                    }
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Label { text: "额外工具"; color: panel.t.muted; font.pixelSize: 12 }
                        TextField {
                            id: toolQueryField
                            objectName: "skillToolQuery"
                            Layout.fillWidth: true
                            placeholderText: "搜索工具名，例如 screenplay 或 read_scene"
                            onTextEdited: panel.toolQuery = text
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        color: panel.t.muted
                        font.pixelSize: 10
                        text: "业务组已经能覆盖的就不必再单点工具；上面搜到的名字直接点它加入。"
                            + "没有搜索时只列已选的工具。"
                    }
                    ScrollView {
                        id: toolScroll
                        Layout.fillWidth: true
                        Layout.preferredHeight: 120
                        clip: true
                        // 工具名是单列竖排：横向滚动条在这里同样只会和竖条互相挤。
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                        Column {
                            width: toolScroll.availableWidth
                            spacing: 2

                            Repeater {
                                model: panel.toolChoices()
                                delegate: Rectangle {
                                    id: toolRow
                                    required property var modelData
                                    readonly property bool picked: panel.isToolChecked(modelData)

                                    objectName: "skillTool_" + modelData
                                    width: toolScroll.availableWidth
                                    height: 22
                                    radius: 4
                                    color: toolRow.picked ? panel.t.accentSoft : "transparent"

                                    Label {
                                        anchors.left: parent.left
                                        anchors.leftMargin: 6
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: (toolRow.picked ? "✓ " : "＋ ") + toolRow.modelData
                                        color: toolRow.picked ? panel.t.accent : panel.t.ink
                                        font.pixelSize: 11
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: panel.toggleTool(toolRow.modelData)
                                    }
                                }
                            }
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: panel.toolChoices().length === 0
                        wrapMode: Text.Wrap
                        color: panel.t.muted
                        font.pixelSize: 10
                        text: panel.toolQuery.trim().length === 0
                            ? "还没有额外工具：在上面搜索工具名加入。"
                            : "没有匹配的工具名，换个关键词试试。"
                    }

                    Label {
                        objectName: "skillOpensPreview"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        color: panel.previewProblem.length > 0 ? panel.t.bad : panel.t.ink
                        font.pixelSize: 11
                        text: panel.previewText()
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
                    text: "保存后立刻写进技能文件；要在会话里生效还需要助手调用 load_skill。"
                }
                Button {
                    objectName: "skillCancel"
                    text: "取消"
                    flat: true
                    onClicked: editor.close()
                }
                Button {
                    objectName: "skillSave"
                    text: "保存"
                    highlighted: true
                    enabled: panel.draftName.trim().length > 0
                        && panel.draftSummary.trim().length > 0
                        && panel.draftInstructions.trim().length > 0
                        && !panel.busy
                    onClicked: panel.commitEditor()
                }
            }
        }
    }
}
