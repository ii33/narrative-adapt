import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 镜头表：逐镜制作驾驶舱。
// 左边一镜一行看状态（时长、画面摘要、关键帧/段/缺口计数、四个产物、回执），
// 右边选中一镜看细节（镜内时间轴标尺、关键帧计划、资产需求与绑定、缺口、回执）。
//
// 为什么数据全走 bridge：形态判定（有没有框图、缺口算几条、时间轴怎么分段）都是
// 业务口径，Python 侧算好了再交给这里画；QML 只负责显示，不解析 storyboard 结构。
//
// 这一页**不做流程门禁**：列、按钮、四个状态一直可见——没有框图也照常显示素描状态，
// 也不存在「先做什么才能做什么」的开关。产图按钮本轮只是占位（执行器在另一路并行
// 开发），禁用并标注「待接入」，点了也不会假装开始。
Item {
    id: page
    objectName: "shotTablePage"
    property var app
    readonly property var t: app.theme

    // -- 数据 ------------------------------------------------------------
    // workspace 只用来列场次（dialog 与分镜稿页同一份 screenplay_workspace 结果）。
    property var workspace: ({})
    property string unitId: ""
    property var table: ({})
    // 行与筛选后的行都是派生值：表格本体由 bridge.shotTable 一次性给出，这里不再改它。
    property var rows: table.rows || []
    property string selectedShotId: ""
    property var detail: ({})
    // 详情面板里「此刻看哪一帧」：只读展示用，点标尺换时刻，不写任何数据。
    property var frame: ({})
    property int frameOffset: 0
    property int filterIndex: 0
    property string message: ""
    property string projectPath: ""

    readonly property bool ready: table.ok === true && !!unitId
    readonly property int totalMs: table.total_duration_ms || 0

    // 列宽只写一份：表头与每一行共用，改一处两边一起动，不会悄悄错位。
    readonly property int colIndex: 44
    readonly property int colDuration: 66
    readonly property int colCount: 54
    readonly property int colStatus: 128
    readonly property int colActions: 96

    readonly property var scenes: (workspace.document?.segments || []).reduce((all, segment) =>
        all.concat((segment.units || []).map(u => ({unit: u, segment: segment.id,
            label: (u.scene_number ? u.scene_number + " · " : "") + (u.plan?.title || u.id)}))), [])

    // 筛选只改变「看到哪几行」，行本身与镜头顺序不变。
    readonly property var filters: [
        {key: "all", label: "全部"},
        {key: "no_layout", label: "未出框图"},
        {key: "no_sketch", label: "未出素描"},
        {key: "gaps", label: "缺口未落实"}
    ]
    readonly property string filterKey: filters[filterIndex].key
    readonly property var filteredRows: rows.filter(function (row) {
        if (filterKey === "no_layout") return !(row.has && row.has.layout)
        if (filterKey === "no_sketch") return !(row.has && row.has.sketch)
        if (filterKey === "gaps") return (row.gap_count || 0) > 0
        return true
    })

    function seconds(ms) { return (ms / 1000).toFixed(2) + " s" }

    // -- 读取 ------------------------------------------------------------
    // 场次清单来自剧本工作区（与分镜稿页同一份数据），镜头数据来自三个只读槽位。
    function loadWorkspace() {
        if (!app || app.mode !== "workspace" || !visible) return
        bridge.loadScreenplayWorkspace(app.screenplayId || "")
    }

    function selectScene(id) {
        if (id === unitId) return
        unitId = id
        selectedShotId = ""
        detail = ({})
        frame = ({})
        frameOffset = 0
        reload()
    }

    function reload() {
        message = ""
        if (!unitId) {
            table = ({})
            detail = ({})
            frame = ({})
            return
        }
        // 槽位是同步的：返回值就是快照，深拷贝一份，免得 QML 侧改到桥里的对象。
        table = JSON.parse(JSON.stringify(bridge.shotTable(unitId)))
        if (table.ok !== true) {
            message = table.error || "读不到这一场的镜头"
            detail = ({})
            frame = ({})
            return
        }
        // 选中的镜可能已经被删掉或换了场次：退到第一行，而不是留着空详情。
        var keep = rows.some(function (row) { return row.shot_id === selectedShotId })
        if (!keep) selectedShotId = rows.length ? rows[0].shot_id : ""
        loadDetail()
    }

    function select(shotId) {
        if (!shotId || shotId === selectedShotId) return
        selectedShotId = shotId
        frameOffset = 0
        loadDetail()
    }

    function loadDetail() {
        if (!unitId || !selectedShotId) {
            detail = ({})
            frame = ({})
            return
        }
        detail = JSON.parse(JSON.stringify(bridge.shotDetail(unitId, selectedShotId)))
        if (detail.ok !== true) message = detail.error || "读不到这一镜"
        var span = (detail.timeline || {}).duration_ms || 0
        if (frameOffset < 0 || frameOffset >= span) frameOffset = 0
        loadFrame()
    }

    function loadFrame() {
        if (!unitId || !selectedShotId) {
            frame = ({})
            return
        }
        frame = JSON.parse(JSON.stringify(bridge.shotLayout(unitId, selectedShotId, frameOffset)))
    }

    // 只读：只是把「此刻」挪到某个时刻，看那一帧有没有框图。拖动留给 E3。
    function pickOffset(ms) {
        frameOffset = Math.max(0, Math.round(ms))
        loadFrame()
    }

    // 行内「刷新数据」：重新读表与详情，选中镜保持不变（丢了就退到第一行）。
    function refreshRow(shotId) {
        if (shotId) selectedShotId = shotId
        reload()
    }

    // -- 展示用的小映射（只是把 Python 给的布尔值配上说法） -----------------
    // 四个产物：没有就说明为什么——「缺」本身就是信息，不该只画一个灰点。
    function productRows() {
        var has = detail.has || ({})
        return [
            {key: "layout", label: "框图", mark: "框", on: has.layout === true,
             reason: has.layout === true ? "已有框图：这一镜的帧写下了机位与主体框"
                 : "还没有框图：没有任何一帧写下站位、机位与景深"},
            {key: "sketch", label: "素描", mark: "素", on: has.sketch === true,
             reason: has.sketch === true ? "已有素描：由图像服务生成过"
                 : "还没有素描：框图只是构图记录，素描才是画出来的那一张"},
            {key: "style", label: "风格", mark: "风", on: has.style === true,
             reason: has.style === true ? "已有风格图"
                 : "还没有风格图：定案后在有草图的帧上渲染"},
            {key: "video", label: "视频", mark: "频", on: has.video === true,
             reason: has.video === true ? "本镜已采用视频"
                 : "还没有采用视频；执行器接入前这里只报事实"}
        ]
    }

    function marksFor(row) {
        var has = (row && row.has) || ({})
        return [{mark: "框", on: has.layout === true},
                {mark: "素", on: has.sketch === true},
                {mark: "风", on: has.style === true},
                {mark: "频", on: has.video === true}]
    }

    // 定位预演：预演页（DirectorWorkspacePage）暂时不接参数，所以这里做两件事——
    // 把这一镜记进 app 级的选择记录（将来预演页接参数时它已经在），然后切页。
    // 现在它只是「切到预演页」，不会带选区进去。
    function openPrevis(shotId) {
        var id = shotId || selectedShotId
        if (!unitId || !id) return
        app.storyboardSelection = {project_path: app.project.path, screenplay_id: app.screenplayId,
                                   unit_id: unitId, shot_id: id, offset_ms: frameOffset}
        app.goTo("previs")
    }

    // 懒创建：创建时一出生就可见，不会再触发 onVisibleChanged。
    Component.onCompleted: if (visible && app) loadWorkspace()
    onVisibleChanged: if (visible && app) loadWorkspace()

    // 换工程时把旧场次与旧快照全部清掉：留着上一场的镜头最容易看错。
    Connections {
        target: app
        function onProjectChanged() {
            var path = page.app.project.path || ""
            if (page.projectPath === path) return
            page.projectPath = path
            page.unitId = ""
            page.table = ({})
            page.detail = ({})
            page.frame = ({})
            page.selectedShotId = ""
            page.loadWorkspace()
        }
    }

    Connections {
        target: bridge
        function onResultReady(kind, payload) {
            if (kind !== "screenplay_workspace" || payload.superseded || !page.visible) return
            page.workspace = payload
            var screenplayId = payload.document?.screenplay?.id || ""
            page.app.screenplayId = screenplayId
            var saved = page.app.screenplaySelection
            var prior = saved && saved.screenplay_id === screenplayId
                && saved.project_path === page.app.project.path ? saved.unit_id : ""
            var found = page.scenes.find(s => s.unit.id === prior)
                || page.scenes.find(s => s.unit.id === page.unitId)
                || page.scenes[0]
            page.selectScene(found ? found.unit.id : "")
        }
        function onErrorOccurred(kind, text) {
            if (kind === "shot_table" && page.visible) page.message = text
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 7

        // -- 顶栏：场次 / 总时长 / 筛选 / 刷新 ------------------------------
        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Label { text: "镜头表"; font.pixelSize: 21; font.bold: true; color: page.t.ink }

            ComboBox {
                objectName: "shotTableScreenplay"
                Layout.preferredWidth: 150
                model: page.workspace.screenplays || []
                textRole: "title"
                currentIndex: model.findIndex(s => s.id === page.app.screenplayId)
                onActivated: { page.app.screenplayId = model[currentIndex].id; page.loadWorkspace() }
            }
            ComboBox {
                objectName: "shotTableScene"
                Layout.preferredWidth: 210
                textRole: "label"
                model: page.scenes
                currentIndex: page.scenes.findIndex(s => s.unit.id === page.unitId)
                onActivated: page.selectScene(model[currentIndex].unit.id)
            }
            Label {
                objectName: "shotTableTotal"
                text: "总时长 " + page.seconds(page.totalMs)
                color: page.t.muted
                font.pixelSize: 12
            }
            ComboBox {
                objectName: "shotTableFilter"
                Layout.preferredWidth: 124
                model: page.filters
                textRole: "label"
                currentIndex: page.filterIndex
                onActivated: page.filterIndex = currentIndex
            }
            Label {
                objectName: "shotTableCounts"
                text: page.filteredRows.length + " / " + page.rows.length + " 镜"
                    + (page.table.unit_gap_count > 0 ? " · 场级缺口 " + page.table.unit_gap_count : "")
                color: page.t.muted
                font.pixelSize: 11
            }
            Button {
                objectName: "shotTableReload"
                text: "刷新"
                enabled: !!page.unitId
                onClicked: page.reload()
            }
            Item { Layout.fillWidth: true }
            WindowChrome { app: page.app }
        }

        Label {
            objectName: "shotTableMessage"
            Layout.fillWidth: true
            visible: text.length > 0
            text: page.message
            color: page.t.warn
            font.pixelSize: 11
            wrapMode: Text.Wrap
        }
        Label {
            Layout.fillWidth: true
            visible: page.scenes.length === 0
            text: "先在剧本里建场次，才有镜头表。"
            color: page.t.muted
            font.pixelSize: 12
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 10

            // -- 主表 ------------------------------------------------------
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: page.t.card
                border.color: page.t.line
                radius: page.t.radius

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 2

                    // 表头
                    RowLayout {
                        objectName: "shotTableHeader"
                        Layout.fillWidth: true
                        spacing: 4
                        Label { Layout.preferredWidth: page.colIndex; text: "镜号"; color: page.t.muted; font.pixelSize: 11 }
                        Label { Layout.preferredWidth: page.colDuration; text: "时长"; color: page.t.muted; font.pixelSize: 11 }
                        Label { Layout.fillWidth: true; text: "画面摘要"; color: page.t.muted; font.pixelSize: 11 }
                        Label { Layout.preferredWidth: page.colCount; text: "关键帧"; color: page.t.muted; font.pixelSize: 11; horizontalAlignment: Text.AlignRight }
                        Label { Layout.preferredWidth: page.colCount; text: "段"; color: page.t.muted; font.pixelSize: 11; horizontalAlignment: Text.AlignRight }
                        Label { Layout.preferredWidth: page.colCount; text: "缺口"; color: page.t.muted; font.pixelSize: 11; horizontalAlignment: Text.AlignRight }
                        Label { Layout.preferredWidth: page.colStatus; text: "框图 素描 风格 视频"; color: page.t.muted; font.pixelSize: 10 }
                        Label { Layout.preferredWidth: page.colCount; text: "回执"; color: page.t.muted; font.pixelSize: 11; horizontalAlignment: Text.AlignRight }
                        Label { Layout.preferredWidth: page.colActions; text: "动作"; color: page.t.muted; font.pixelSize: 11 }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "实心圆点＝已有；标记读作框图 / 素描 / 风格 / 视频。"
                        color: page.t.muted
                        font.pixelSize: 10
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: page.t.line }

                    ListView {
                        id: shotList
                        objectName: "shotTableList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 2
                        model: page.filteredRows

                        delegate: Rectangle {
                            required property int index
                            required property var modelData
                            objectName: "shotTableRow"
                            // 选中镜可被点选：右侧详情的来源就是这一行。
                            property string shotId: modelData.shot_id
                            property int shotIndex: modelData.index
                            width: ListView.view ? ListView.view.width : 0
                            height: 34
                            radius: 6
                            color: modelData.shot_id === page.selectedShotId ? page.t.accentSoft
                                 : rowHover.hovered ? page.t.hover : "transparent"

                            HoverHandler { id: rowHover }

                            // 整行的点击先声明：后面那排按钮盖在它上面，点按钮不会被它吃掉。
                            MouseArea {
                                objectName: "shotTableRowHit"
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: page.select(modelData.shot_id)
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 6
                                anchors.rightMargin: 6
                                spacing: 4

                                Label {
                                    objectName: "shotTableRowIndex"
                                    Layout.preferredWidth: page.colIndex
                                    text: modelData.index
                                    color: page.t.ink
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                }
                                Label {
                                    Layout.preferredWidth: page.colDuration
                                    text: page.seconds(modelData.duration_ms)
                                    color: page.t.body
                                    font.pixelSize: 12
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: modelData.summary || modelData.title
                                    color: page.t.ink
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                                Label {
                                    Layout.preferredWidth: page.colCount
                                    text: modelData.keyframe_count
                                    color: page.t.muted
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignRight
                                }
                                Label {
                                    Layout.preferredWidth: page.colCount
                                    text: modelData.segment_count
                                    color: page.t.muted
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignRight
                                }
                                Label {
                                    Layout.preferredWidth: page.colCount
                                    text: modelData.gap_count
                                    color: modelData.gap_count > 0 ? page.t.warn : page.t.muted
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignRight
                                }
                                // 四个产物：小圆点＋一个字，不用 emoji（字体不一定有，宽度也会跳）。
                                Row {
                                    Layout.preferredWidth: page.colStatus
                                    spacing: 6
                                    Repeater {
                                        model: page.marksFor(modelData)
                                        delegate: Row {
                                            required property var modelData
                                            spacing: 2
                                            Rectangle {
                                                width: 7; height: 7; radius: 3.5
                                                anchors.verticalCenter: parent.verticalCenter
                                                color: modelData.on ? page.t.ok : "transparent"
                                                border.width: 1
                                                border.color: modelData.on ? page.t.ok : page.t.muted
                                            }
                                            Label {
                                                text: modelData.mark
                                                font.pixelSize: 10
                                                color: modelData.on ? page.t.ok : page.t.muted
                                            }
                                        }
                                    }
                                }
                                Label {
                                    Layout.preferredWidth: page.colCount
                                    text: modelData.receipts
                                    color: page.t.muted
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignRight
                                }
                                RowLayout {
                                    Layout.preferredWidth: page.colActions
                                    spacing: 2
                                    ToolButton {
                                        objectName: "shotTableRowPrevis"
                                        text: "预演"
                                        font.pixelSize: 11
                                        implicitWidth: 44
                                        implicitHeight: 24
                                        enabled: !!page.unitId
                                        ToolTip.visible: hovered
                                        ToolTip.text: "定位预演：切到预演页，并记下这一镜（预演页暂不接参数）"
                                        onClicked: page.openPrevis(modelData.shot_id)
                                    }
                                    ToolButton {
                                        objectName: "shotTableRowRefresh"
                                        text: "刷新"
                                        font.pixelSize: 11
                                        implicitWidth: 44
                                        implicitHeight: 24
                                        ToolTip.visible: hovered
                                        ToolTip.text: "重新读这一镜的数据"
                                        onClicked: page.refreshRow(modelData.shot_id)
                                    }
                                }
                            }
                        }
                    }

                    Label {
                        objectName: "shotTableEmpty"
                        Layout.fillWidth: true
                        visible: page.filteredRows.length === 0
                        text: page.rows.length === 0
                            ? "这一场还没有镜头：去分镜稿排第一镜。"
                            : "当前筛选下没有镜头。"
                        color: page.t.muted
                        font.pixelSize: 12
                    }
                }
            }

            // -- 右侧详情 ---------------------------------------------------
            Rectangle {
                Layout.preferredWidth: 348
                Layout.minimumWidth: 280
                Layout.fillHeight: true
                color: page.t.card
                border.color: page.t.line
                radius: page.t.radius

                Label {
                    anchors.centerIn: parent
                    width: parent.width - 40
                    visible: !page.detail.ok
                    text: "选中左边任意一行，看这一镜的时间轴、需求与缺口。"
                    color: page.t.muted
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignHCenter
                }

                ScrollView {
                    id: detailScroll
                    objectName: "shotDetailScroll"
                    anchors.fill: parent
                    anchors.margins: 10
                    contentWidth: availableWidth
                    clip: true
                    visible: page.detail.ok === true

                    ColumnLayout {
                        objectName: "shotDetail"
                        width: detailScroll.availableWidth
                        spacing: 8

                        // 标题：镜号 · 标题，下面是时长与正文依据数
                        Label {
                            objectName: "shotDetailTitle"
                            Layout.fillWidth: true
                            text: page.detail.ok ? (page.detail.shot.index + " · " + page.detail.shot.title) : ""
                            color: page.t.ink
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                            wrapMode: Text.Wrap
                        }
                        Label {
                            objectName: "shotDetailMeta"
                            Layout.fillWidth: true
                            text: page.detail.ok
                                ? "时长 " + page.seconds(page.detail.shot.duration_ms)
                                  + " · 正文依据 " + (page.detail.shot.source_block_ids || []).length + " 块"
                                  + (page.detail.handoff ? " · 衔接自 " + page.detail.handoff.from_shot : "")
                                : ""
                            color: page.t.muted
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "画面：" + ((page.detail.shot || {}).description || "（尚未写画面描述）")
                            color: page.t.body
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }

                        // -- 镜内时间轴标尺（只读） ---------------------------
                        Label {
                            Layout.fillWidth: true
                            text: "镜内时间轴（只读）"
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Rectangle {
                            id: ruler
                            objectName: "shotDetailRuler"
                            Layout.fillWidth: true
                            Layout.preferredHeight: 56
                            color: page.t.panelAlt
                            border.color: page.t.line
                            radius: 4
                            clip: true

                            readonly property var timeline: page.detail.timeline || ({})
                            readonly property real span: Math.max(1, timeline.duration_ms || 1)

                            // 段：相邻关键帧之间的一段；交替底色，节奏一眼能数。
                            Repeater {
                                model: ruler.timeline.segments || []
                                delegate: Rectangle {
                                    required property var modelData
                                    required property int index
                                    x: ruler.width * modelData[0] / ruler.span
                                    width: Math.max(0, ruler.width * (modelData[1] - modelData[0]) / ruler.span)
                                    height: ruler.height
                                    color: index % 2 === 0 ? page.t.card : page.t.hover
                                    opacity: 0.9
                                }
                            }
                            // 关键帧：竖线＋时刻；点一下就把「此刻」挪到那里（只读）。
                            Repeater {
                                model: ruler.timeline.keyframes || []
                                delegate: Item {
                                    required property var modelData
                                    width: 46
                                    height: ruler.height
                                    x: Math.max(0, Math.min(ruler.width - width,
                                                            ruler.width * modelData.offset_ms / ruler.span))
                                    Rectangle { width: 2; height: ruler.height; color: page.t.accent }
                                    Label {
                                        x: 3; y: 1
                                        text: page.seconds(modelData.offset_ms)
                                        color: page.t.accent
                                        font.pixelSize: 9
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: page.pickOffset(modelData.offset_ms)
                                    }
                                }
                            }
                            // 声音节拍：方块；时刻要先落到尺上再画，null 的节拍不画。
                            Repeater {
                                model: (ruler.timeline.sound_cues || []).filter(
                                    function (cue) { return cue.offset_ms !== null && cue.offset_ms !== undefined })
                                delegate: Rectangle {
                                    required property var modelData
                                    width: 7; height: 7
                                    y: 26
                                    x: Math.max(0, Math.min(ruler.width - width,
                                                            ruler.width * modelData.offset_ms / ruler.span))
                                    color: page.t.warn
                                }
                            }
                            // 机位点：小三角位置用窄矩形代替（不引图形资源）。
                            Repeater {
                                model: ruler.timeline.camera_keys || []
                                delegate: Rectangle {
                                    required property var modelData
                                    width: 3; height: 11
                                    y: 38
                                    x: Math.max(0, Math.min(ruler.width - width,
                                                            ruler.width * modelData.offset_ms / ruler.span))
                                    color: page.t.handle
                                }
                            }
                            // 此刻：详情面板正在看的那一帧
                            Rectangle {
                                width: 1
                                height: ruler.height
                                visible: (page.detail.timeline || {}).duration_ms > 0
                                x: Math.max(0, Math.min(ruler.width - width,
                                                        ruler.width * page.frameOffset / ruler.span))
                                color: page.t.ink
                                opacity: 0.55
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "竖线＝关键帧　方块＝声音节拍　细条＝机位点　深线＝此刻（点击关键帧或方框挪动）"
                                + (((page.detail.timeline_derived || {}).segments === true)
                                   ? "　分段为推导值" : "")
                            color: page.t.muted
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }

                        // -- 这一帧的框图与草图 ---------------------------------
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Label {
                                objectName: "shotDetailFrameState"
                                Layout.fillWidth: true
                                // 框图是结构化记录（机位、主体框、这一帧还缺什么），不是图片；
                                // 有就说清是哪一种之后有什么，没有就说清为什么没有。
                                text: page.frame.ok === true
                                    ? "此刻 " + page.seconds(page.frame.frame_offset_ms) + " 有框图 · "
                                      + (page.frame.camera.framing || "未写景别")
                                      + " · " + (page.frame.objects || []).length + " 个主体"
                                      + " · 来源 " + (page.frame.layout.source || "manual")
                                      + " · " + page.frame.hash
                                    : "此刻没有框图：" + (page.frame.reason || "")
                                color: page.frame.ok === true ? page.t.ok : page.t.muted
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }
                        // 框图里这一帧还缺的资产：帧级需求，和镜级需求分开放。
                        Repeater {
                            model: page.frame.unmet_needs || []
                            delegate: Label {
                                required property var modelData
                                objectName: "shotDetailLayoutNeedRow"
                                Layout.fillWidth: true
                                text: "这一帧还缺 · " + modelData.kind + " · " + modelData.name
                                color: page.t.warn
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                        }
                        // 草图预览：这是像素产物（素描／风格图），别和上面的框图混为一谈。
                        Label {
                            Layout.fillWidth: true
                            visible: page.frame.ok === true || (page.frame.sketch_url || "").length > 0
                            text: "草图预览（该帧素描）"
                            color: page.t.muted
                            font.pixelSize: 10
                        }
                        Image {
                            objectName: "shotDetailFrameImage"
                            visible: (page.frame.sketch_url || "").length > 0
                            source: visible ? page.frame.sketch_url : ""
                            Layout.fillWidth: true
                            Layout.preferredHeight: 132
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            cache: false
                        }

                        // -- 关键帧计划 ---------------------------------------
                        Label {
                            Layout.fillWidth: true
                            text: "关键帧计划（时刻 · 命名 · 描述）"
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: ((page.detail.timeline || {}).keyframes || []).length === 0
                            text: "本镜还没有关键帧计划。"
                            color: page.t.muted
                            font.pixelSize: 11
                        }
                        Repeater {
                            model: (page.detail.timeline || {}).keyframes || []
                            delegate: Label {
                                required property var modelData
                                objectName: "shotDetailKeyframeRow"
                                Layout.fillWidth: true
                                text: page.seconds(modelData.offset_ms) + " · "
                                    + (modelData.moment_name || "未命名") + " · "
                                    + (modelData.description || "（无描述）")
                                color: page.t.body
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }

                        // -- 资产需求与绑定 -----------------------------------
                        Label {
                            Layout.fillWidth: true
                            text: "资产需求与绑定"
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Repeater {
                            model: ((page.detail.asset_needs || {}).shot || [])
                                .concat((page.detail.asset_needs || {}).unit || [])
                            delegate: Label {
                                required property var modelData
                                objectName: "shotDetailNeedRow"
                                Layout.fillWidth: true
                                // 绑定是「谁落实了这条需求」；没绑定就是缺口，由下面的缺口清单再点一次。
                                text: (modelData.scope === "unit" ? "场级" : "镜级") + " · "
                                    + modelData.kind + " · " + modelData.name
                                    + (modelData.variant ? "（" + modelData.variant + "）" : "")
                                    + " → " + (modelData.bound_asset_id
                                        ? "已绑定 " + modelData.bound_asset_id : "未落实")
                                color: modelData.bound_asset_id ? page.t.body : page.t.warn
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }
                        Repeater {
                            model: ((page.detail.shot || {}).visual_elements || [])
                            delegate: Label {
                                required property var modelData
                                objectName: "shotDetailElementRow"
                                Layout.fillWidth: true
                                text: "元素 · " + (modelData.label || modelData.name)
                                    + "（" + modelData.kind + "） · "
                                    + (modelData.status === "ready" ? "已绑定可出图"
                                       : modelData.status === "image_missing" ? "已建卡，待加参考图"
                                       : modelData.status === "type_mismatch" ? "元素类型与资产类别不符"
                                       : modelData.status === "missing" ? "绑定的资产卡不存在"
                                       : "未绑定资产卡")
                                color: modelData.status === "ready" ? page.t.body : page.t.warn
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: ((page.detail.asset_needs || {}).shot || []).length === 0
                                && ((page.detail.asset_needs || {}).unit || []).length === 0
                                && ((page.detail.shot || {}).visual_elements || []).length === 0
                            text: "本镜没有声明资产需求，也没有元素绑定。"
                            color: page.t.muted
                            font.pixelSize: 11
                        }

                        // -- 缺口清单 -----------------------------------------
                        Label {
                            Layout.fillWidth: true
                            text: "缺口清单"
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: (page.detail.gaps || []).length === 0
                            text: "本镜没有缺口。"
                            color: page.t.muted
                            font.pixelSize: 11
                        }
                        Repeater {
                            model: page.detail.gaps || []
                            delegate: Label {
                                required property var modelData
                                objectName: "shotDetailGapRow"
                                Layout.fillWidth: true
                                text: (modelData.gap === "dangling" ? "引用悬空" : "需求未落实")
                                    + " · " + modelData.name + " · " + modelData.message
                                color: page.t.warn
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: (page.detail.unit_gaps || []).length > 0
                            text: "另有场级需求未落实 " + (page.detail.unit_gaps || []).length + " 条（全场共用）"
                            color: page.t.muted
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }

                        // -- 四个产物：状态与原因 ------------------------------
                        Label {
                            Layout.fillWidth: true
                            text: "产物状态"
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Repeater {
                            model: page.productRows()
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 6
                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: modelData.on ? page.t.ok : "transparent"
                                    border.width: 1
                                    border.color: modelData.on ? page.t.ok : page.t.muted
                                }
                                Label {
                                    text: modelData.label + " · " + (modelData.on ? "已有" : "未出")
                                    color: modelData.on ? page.t.ok : page.t.muted
                                    font.pixelSize: 11
                                    Layout.preferredWidth: 74
                                }
                                Label {
                                    objectName: "shotDetailProductReason"
                                    Layout.fillWidth: true
                                    text: modelData.reason
                                    color: page.t.muted
                                    font.pixelSize: 10
                                    wrapMode: Text.Wrap
                                }
                            }
                        }

                        // 产图类动作：本轮只占位。禁用＋「待接入」，不做假的成功提示。
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Repeater {
                                model: [{key: "layout", label: "出框图"},
                                        {key: "sketch", label: "出素描"},
                                        {key: "style", label: "出风格图"},
                                        {key: "video", label: "出视频"}]
                                delegate: Button {
                                    required property var modelData
                                    objectName: "shotProduce_" + modelData.key
                                    Layout.fillWidth: true
                                    text: modelData.label
                                    font.pixelSize: 10
                                    enabled: false
                                    ToolTip.visible: hovered
                                    ToolTip.text: "待接入：执行器在另一路并行开发，这一版只占位"
                                }
                            }
                        }

                        // -- 回执 ---------------------------------------------
                        Label {
                            Layout.fillWidth: true
                            text: "回执"
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: (page.detail.receipts || []).length === 0
                            text: "本镜还没有回执。"
                            color: page.t.muted
                            font.pixelSize: 11
                        }
                        Repeater {
                            model: page.detail.receipts || []
                            delegate: Label {
                                required property var modelData
                                objectName: "shotDetailReceiptRow"
                                Layout.fillWidth: true
                                text: modelData.kind === "video"
                                    ? "视频回执 · " + modelData.generation_id + " · " + modelData.id
                                    : "素描回执 · " + page.seconds(modelData.offset_ms)
                                      + " · " + (modelData.provider || "未知服务")
                                      + (modelData.job_id ? " · " + modelData.job_id : "")
                                color: page.t.body
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
        }
    }
}
