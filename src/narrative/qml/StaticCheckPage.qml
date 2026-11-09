import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    objectName: "staticCheckPage"
    property var app
    readonly property var t: app.theme
    property bool running: false
    property bool hasReport: false
    property int activeScanId: 0
    property int latestRevision: 0
    property var report: ({})
    property string failure: ""
    property var rollbackPlan: ({})
    property bool rollbackBusy: false
    property string rollbackMessage: ""
    property string severity: "all"
    readonly property bool stale: hasReport && (report.is_stale === true || latestRevision > report.revision)
    readonly property var visibleIssues: (report.issues || []).filter(function(issue) {
        return page.severity === "all" || issue.severity === page.severity
    })

    function reset() {
        running = false; hasReport = false; report = ({}); failure = ""; activeScanId = 0
        latestRevision = app.project.revision || 0
        rollbackPlan = ({}); rollbackBusy = false; rollbackMessage = ""
    }
    function run() {
        if (running || app.mode !== "workspace") return
        failure = ""
        running = true
        bridge.runStaticCheck()
    }
    function localCheckTime(value) {
        var time = new Date(value || "")
        return isNaN(time.getTime()) ? String(value || "") : Qt.formatDateTime(time, "yyyy-MM-dd HH:mm:ss") + "（本地时间）"
    }
    function severityText(value) {
        return ({error: "错误", warning: "警告", info: "提示"})[value] || value
    }
    function severityColor(value) {
        return value === "error" ? page.t.bad : value === "warning" ? page.t.warn : page.t.muted
    }
    readonly property var targetKindLabels: ({
        property_definition: "属性定义", property_change: "属性取值", node: "节点",
        event_location: "事件地点", participants: "参与者", reference: "引用",
        storyline_range: "故事线区间", project: "工程"
    })
    // 定位字段可能在新的 target 块里，也可能还只写在顶层；两边都读，缺哪个就降级到下一个。
    function issueTarget(issue) {
        var row = issue || ({})
        var target = row.target || ({})
        function pick(key) {
            var value = target[key]
            return value === undefined || value === null || value === "" ? row[key] : value
        }
        return {
            kind: String(target.kind || ""),
            propertyId: String(pick("property_id") || ""),
            nodeId: String(pick("node_id") || ""),
            sourceId: String(pick("source_id") || ""),
            recordId: String(pick("record_id") || ""),
            quote: String(pick("quote") || ""),
            start: pick("start"),
            end: pick("end")
        }
    }
    function issueNodeName(issue) {
        var row = issue || ({})
        return String(row.node_name || row.object_name || row.name || "")
    }
    // 每条问题的来源标签：属性 → 节点 → 原文坐标 → 工程，越具体的越优先。
    function issueSourceText(issue) {
        var target = page.issueTarget(issue)
        var kindLabel = page.targetKindLabels[target.kind] || ""
        var nodeName = page.issueNodeName(issue)
        if (target.propertyId) {
            var named = (kindLabel || "属性") + "「" + target.propertyId + "」"
            return target.nodeId ? named + " · 节点 " + (nodeName || target.nodeId) : named
        }
        if (target.nodeId)
            return "节点 " + (nodeName || target.nodeId) + (nodeName ? "（" + target.nodeId + "）" : "")
        if (target.sourceId) {
            var offset = target.start === undefined || target.start === null ? "" : " · 偏移 " + target.start
            if (offset && target.end !== undefined && target.end !== null) offset += "–" + target.end
            return "原文 " + target.sourceId + offset
        }
        return "工程"
    }
    function issueJumpable(issue) {
        var target = page.issueTarget(issue)
        if (target.kind === "project") return false
        return !!target.nodeId || !!target.sourceId || !!target.propertyId
    }
    // 定位接口读顶层字段，这里把 target 里的字段摊回顶层，老格式（字段直接在顶层）原样通过。
    function issueForJump(issue) {
        var merged = ({})
        var row = issue || ({})
        for (var key in row) merged[key] = row[key]
        var target = page.issueTarget(issue)
        if (target.nodeId) merged.node_id = target.nodeId
        if (target.sourceId) merged.source_id = target.sourceId
        if (target.propertyId) merged.property_id = target.propertyId
        if (target.quote) merged.quote = target.quote
        if (target.start !== undefined && target.start !== null) merged.start = target.start
        if (target.end !== undefined && target.end !== null) merged.end = target.end
        return merged
    }
    // 有节点跳节点、有原文坐标跳原文；只有属性时按属性定义记录聚焦（浏览页没有按属性筛的入口）。
    function jumpIssue(issue) {
        if (!page.issueJumpable(issue)) return
        var target = page.issueTarget(issue)
        var merged = page.issueForJump(issue)
        if (target.nodeId) { bridge.resolveStaticIssue(merged, "node", ""); return }
        if (target.sourceId) { bridge.resolveStaticIssue(merged, "source", ""); return }
        bridge.resolveStaticIssue(merged, "record", target.propertyId || target.recordId)
    }

    Connections {
        target: bridge
        function onResultReady(kind, payload) {
            if (kind === "project" || kind === "project_closed") page.reset()
            else if (kind === "rollback_preview") {
                page.rollbackBusy = false; page.rollbackPlan = payload
                page.rollbackMessage = "修订 " + payload.expected_revision
                    + "：将删除 " + (payload.deleted_node_ids || []).length + " 个节点、"
                    + (payload.deleted_record_ids || []).length + " 条属性记录；重新开放 "
                    + (payload.reopened_record_ids || []).length + " 条取值有效期。"
                    + (payload.valid ? "" : " 预检未通过：" + JSON.stringify(payload.diagnostics || []))
            } else if (kind === "rollback") {
                page.rollbackBusy = false; page.rollbackPlan = ({})
                page.rollbackMessage = payload.status === "accepted"
                    ? "已按原文位置回退，当前修订 " + payload.revision
                    : "回退未通过静态校验：" + JSON.stringify(payload.diagnostics || [])
            } else if (kind === "static_check_started") {
                page.activeScanId = payload.scan_id
                page.running = true; page.hasReport = false; page.report = ({}); page.failure = ""
            } else if (kind === "static_check" && payload.scan_id === page.activeScanId) {
                page.running = false
                if (payload.failed) { page.failure = payload.error || "检查未完成"; return }
                page.report = payload
                page.hasReport = true
                page.latestRevision = Math.max(page.latestRevision, payload.current_revision || payload.revision)
            } else if (kind === "static_check_revision") {
                page.latestRevision = payload.revision
            } else if (payload.revision !== undefined && kind !== "static_check") {
                page.latestRevision = Math.max(page.latestRevision, payload.revision)
            }
        }
        function onErrorOccurred(kind, message) {
            if (kind === "static_check") { page.running = false; page.failure = message }
            else if (kind === "static_check_target") page.failure = message
            else if (kind === "rollback_preview" || kind === "rollback") {
                page.rollbackBusy = false; page.rollbackMessage = message
            }
        }
    }
    Connections {
        target: app
        function onProjectChanged() { page.latestRevision = Math.max(page.latestRevision, app.project.revision || 0) }
        function onCurrentPageChanged() {
            if (app.currentPage === "static_check" && app.mode === "workspace") bridge.loadStaticCheckRevision()
        }
    }
    // 懒创建：创建时 onCurrentPageChanged 早在建它之前就发过了，这里补一次进入时的刷新。
    Component.onCompleted: if (visible && app.mode === "workspace") bridge.loadStaticCheckRevision()
    Timer {
        interval: 2000
        repeat: true
        running: page.visible && page.hasReport && !page.running && app.mode === "workspace"
        onTriggered: bridge.loadStaticCheckRevision()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 20
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Label { text: "静态检查"; color: page.t.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
            Item { Layout.fillWidth: true }
            BusyIndicator { running: page.running; visible: running; Layout.preferredWidth: 28; Layout.preferredHeight: 28 }
            Button {
                objectName: "staticCheckRun"
                text: page.running ? "检查中…" : "运行检查"
                enabled: !page.running && app.mode === "workspace"
                onClicked: page.run()
            }
        }
        GroupBox {
            title: "按原文位置回退"
            Layout.fillWidth: true
            ColumnLayout {
                width: parent.width
                Label {
                    text: "保留选定位置及之前的数据，物理删除其后数据与后续原文的数据；不会恢复节点旧版本。"
                    wrapMode: Text.Wrap; Layout.fillWidth: true; color: page.t.muted
                }
                Flow {
                    Layout.fillWidth: true; spacing: 6
                    TextField { id: rollbackSource; objectName: "rollbackSource"; placeholderText: "原文编号"; width: 160; onTextChanged: page.rollbackPlan = ({}) }
                    TextField { id: rollbackStart; objectName: "rollbackStart"; placeholderText: "Unicode字符偏移"; width: 160; validator: IntValidator { bottom: 0 } onTextChanged: page.rollbackPlan = ({}) }
                    Button {
                        objectName: "previewRollbackButton"; text: "预览回退范围"
                        enabled: !page.rollbackBusy && rollbackSource.text.trim().length > 0 && rollbackStart.acceptableInput && rollbackStart.text.length > 0
                        onClicked: { page.rollbackBusy = true; bridge.previewRollback(rollbackSource.text.trim(), Number(rollbackStart.text)) }
                    }
                    Button {
                        objectName: "executeRollbackButton"; text: "执行回退"
                        enabled: !page.rollbackBusy && page.rollbackPlan.valid === true && page.rollbackPlan.expected_revision !== undefined
                        onClicked: { page.rollbackBusy = true; bridge.rollbackTo(rollbackSource.text.trim(), Number(rollbackStart.text), page.rollbackPlan.expected_revision, "用户按原文位置回退") }
                    }
                }
                Label { objectName: "rollbackMessage"; text: page.rollbackMessage; visible: text.length > 0; wrapMode: Text.Wrap; Layout.fillWidth: true; color: page.t.ink }
            }
        }
        Label {
            Layout.fillWidth: true
            text: "检查证据、引用、结构与属性约束，只读运行；未发现静态问题不代表原文已完整提取。"
            color: page.t.muted
            wrapMode: Text.Wrap
        }
        Label {
            objectName: "staticCheckRevision"
            Layout.fillWidth: true
            visible: page.hasReport
            text: "检查修订 " + page.report.revision + " · 当前修订 " + page.latestRevision
                + " · " + page.localCheckTime(page.report.checked_at)
            color: page.t.muted; wrapMode: Text.Wrap
        }
        Label {
            objectName: "staticCheckStale"
            Layout.fillWidth: true
            visible: page.stale
            text: "检查结果已过期：工程数据已变更，请重新运行。"
            color: page.t.warn; wrapMode: Text.Wrap
        }
        Label {
            objectName: "staticCheckFailure"
            Layout.fillWidth: true
            visible: page.failure.length > 0
            text: "检查或定位失败：" + page.failure
            color: page.t.bad; wrapMode: Text.Wrap
        }
        RowLayout {
            visible: page.hasReport
            Label {
                objectName: "staticCheckCounts"
                text: "错误 " + ((page.report.counts || {}).error || 0)
                    + " · 警告 " + ((page.report.counts || {}).warning || 0)
                    + " · 提示 " + ((page.report.counts || {}).info || 0)
                color: page.t.body
            }
            ComboBox {
                id: filter
                objectName: "staticCheckSeverity"
                textRole: "label"
                model: [{label: "全部", key: "all"}, {label: "错误", key: "error"},
                        {label: "警告", key: "warning"}, {label: "提示", key: "info"}]
                onActivated: page.severity = model[currentIndex].key
            }
        }
        Label {
            objectName: "staticCheckEmpty"
            Layout.fillWidth: true
            visible: !page.running && page.failure.length === 0 && (!page.hasReport || page.visibleIssues.length === 0)
            text: !page.hasReport ? "尚未运行静态检查。"
                : (page.report.issues || []).length === 0 ? "本次检查未发现静态问题。" : "该级别没有问题。"
            color: page.t.muted
        }
        ListView {
            objectName: "staticCheckIssues"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 10
            model: page.visibleIssues
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                id: issueRow
                objectName: "staticCheckIssue"
                required property var modelData
                readonly property var issue: modelData
                readonly property bool jumpable: page.issueJumpable(issue)
                readonly property var jumpTarget: page.issueTarget(issue)
                width: ListView.view.width
                height: content.implicitHeight + 20
                radius: 6
                color: page.t.card; border.color: page.t.line
                ColumnLayout {
                    id: content
                    anchors.left: parent.left; anchors.right: parent.right
                    anchors.top: parent.top; anchors.margins: 10
                    spacing: 6
                    Rectangle {
                        id: issueHeader
                        Layout.fillWidth: true
                        implicitHeight: headerRow.implicitHeight + 6
                        radius: 4
                        color: headerHover.hovered && issueRow.jumpable ? page.t.hover : "transparent"
                        RowLayout {
                            id: headerRow
                            anchors.left: parent.left; anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.leftMargin: 2; anchors.rightMargin: 2
                            spacing: 8
                            Label {
                                objectName: "staticCheckIssueSource"
                                Layout.fillWidth: true
                                text: page.severityText(issueRow.issue.severity) + " · " + page.issueSourceText(issueRow.issue)
                                color: page.severityColor(issueRow.issue.severity)
                                wrapMode: Text.Wrap
                                font.weight: Font.DemiBold
                            }
                            Label {
                                objectName: "staticCheckIssueCode"
                                text: issueRow.issue.code || ""
                                font.pixelSize: 11
                                color: page.t.muted
                                topPadding: 3; bottomPadding: 3; leftPadding: 6; rightPadding: 6
                                background: Rectangle { radius: 4; color: page.t.panelAlt; border.color: page.t.line }
                            }
                        }
                        HoverHandler {
                            id: headerHover
                            cursorShape: issueRow.jumpable ? Qt.PointingHandCursor : Qt.ArrowCursor
                        }
                        TapHandler {
                            enabled: issueRow.jumpable
                            onTapped: page.jumpIssue(issueRow.issue)
                        }
                    }
                    Label { Layout.fillWidth: true; text: issueRow.issue.message || ""; wrapMode: Text.Wrap; color: page.t.body }
                    Flow {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        spacing: 6
                        Button {
                            objectName: "staticCheckNode"
                            visible: !!issueRow.jumpTarget.nodeId
                            text: "查看节点"
                            onClicked: bridge.resolveStaticIssue(page.issueForJump(issueRow.issue), "node", "")
                        }
                        Button {
                            objectName: "staticCheckProperty"
                            visible: !!issueRow.jumpTarget.propertyId && !issueRow.jumpTarget.nodeId
                                && !issueRow.jumpTarget.sourceId
                            text: "查看属性定义"
                            onClicked: bridge.resolveStaticIssue(page.issueForJump(issueRow.issue), "record",
                                issueRow.jumpTarget.propertyId || issueRow.jumpTarget.recordId)
                        }
                        Repeater {
                            model: issueRow.issue.record_ids || []
                            Button {
                                objectName: "staticCheckRecord"
                                required property string modelData
                                text: "属性记录 " + modelData
                                onClicked: bridge.resolveStaticIssue(page.issueForJump(issueRow.issue), "record", modelData)
                            }
                        }
                        Button {
                            objectName: "staticCheckSource"
                            visible: !!issueRow.jumpTarget.sourceId
                            text: "查看原文引文"
                            onClicked: bridge.resolveStaticIssue(page.issueForJump(issueRow.issue), "source", "")
                        }
                    }
                }
            }
        }
    }
}
