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
// 也不存在「先做什么才能做什么」的开关。产图按钮接在真实执行器上（生成失败只报错，
// 旧图保留）；镜内标尺可拖，但只有关键帧点与段边界能拖，能不能拖与为什么不能都由
// 桥算好（draggable / reason），这里不重新推断规则。
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
    // 详情面板里「此刻看哪一帧」：点标尺换时刻，不写任何数据；拖完关键帧则跟着新时刻走。
    property var frame: ({})
    property int frameOffset: 0
    property int filterIndex: 0
    property string message: ""
    // 成功也要说一句：只说失败的界面，成功看起来跟没反应一样。
    property string notice: ""
    // 拖动过程中的一句话提示（推导段变显式段、这一点为什么不能拖），不弹对话框。
    property string dragHint: ""
    // 一次只跑一个产图动作：执行器要把网络请求跑完，重复点只会重复花钱。
    property bool producing: false
    property string produceKind: ""
    property var produceResult: ({})
    property string produceHint: ""
    property var previs: ({})
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

    // 这一镜在预演里的绑定：绑没绑机位决定「出框图」能不能走预演投影，
    // 所以信息与按钮放在一起，而不是让用户点一下才看到失败原因。
    readonly property var previsBinding: {
        var rows = (previs.bindings || []).filter(function (row) { return row.shot_id === selectedShotId })
        return rows.length ? (rows[0].previs || ({})) : ({})
    }
    readonly property string produceLabel: ({layout: "框图", sketch: "素描", style: "风格图", video: "视频"})[produceKind] || ""

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
        forgetResults()
        reload()
    }

    // 换镜/换场时把上一次的结果清掉：留着上一镜的「已生成」与路径最容易被看错。
    function forgetResults() {
        notice = ""
        dragHint = ""
        produceResult = ({})
        produceHint = ""
    }

    function reload() {
        message = ""
        if (!unitId) {
            table = ({})
            detail = ({})
            frame = ({})
            previs = ({})
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
        forgetResults()
        loadDetail()
    }

    function loadDetail() {
        if (!unitId || !selectedShotId) {
            detail = ({})
            frame = ({})
            previs = ({})
            return
        }
        detail = JSON.parse(JSON.stringify(bridge.shotDetail(unitId, selectedShotId)))
        if (detail.ok !== true) message = detail.error || "读不到这一镜"
        var span = (detail.timeline || {}).duration_ms || 0
        if (frameOffset < 0 || frameOffset >= span) frameOffset = 0
        loadFrame()
        loadPrevis()
    }

    function loadFrame() {
        if (!unitId || !selectedShotId) {
            frame = ({})
            return
        }
        frame = JSON.parse(JSON.stringify(bridge.shotLayout(unitId, selectedShotId, frameOffset)))
    }

    // 本场有没有预演、有哪些机位、哪几镜绑好了：出框图全靠这条绑定，
    // 所以详情一打开就读一次，而不是等用户点了按钮才告诉他缺什么。
    function loadPrevis() {
        if (!unitId) {
            previs = ({})
            return
        }
        previs = JSON.parse(JSON.stringify(bridge.readPrevis(unitId)))
    }

    // 只读：把「此刻」挪到某个时刻，看那一帧有没有框图。
    function pickOffset(ms) {
        frameOffset = Math.max(0, Math.round(ms))
        loadFrame()
    }

    // -- 写操作的结果只分两种：写成了就说写成了，没写成只说原因 ----------------
    // 绝不静默：一次拖动若既没成功提示也没有错误，用户会以为界面卡住了。
    function applyWriteResult(result, fallback) {
        if (!result) {
            notice = ""
            message = fallback + "失败：桥没有返回结果"
            return false
        }
        if (result.ok === true) {
            message = ""
            notice = result.notice || (fallback + "成功")
            produceHint = ""
            return true
        }
        notice = ""
        message = result.error || (fallback + "失败")
        produceHint = result.hint || ""
        return false
    }

    // -- 拖标尺：松手才提交一次 -----------------------------------------------
    // 为什么不 onValueChanged 连续提交：每写一次都要重读整场分镜，拖一格写十几次，
    // 既慢又会把「改到一半的时刻」写进工程。拖动过程只改标尺上的本地位置。
    function commitKeyframeDrag(shotId, fromMs, toMs) {
        if (!unitId || !shotId) return
        var result = bridge.moveShotKeyframe(unitId, shotId, Math.round(fromMs), Math.round(toMs))
        // 先重读（reload 会清掉 message），再把结果说出来。
        reload()
        if (!applyWriteResult(result, "改关键帧时刻")) return
        var offset = (result.offset_ms === undefined || result.offset_ms === null) ? toMs : Number(result.offset_ms)
        frameOffset = Math.max(0, Math.round(offset))
        loadFrame()
    }

    // 段边界拖动：端点必须落在已有关键帧上，所以实际落点由桥按候选吸附后告诉我们。
    function commitSegmentDrag(shotId, index, toMs) {
        if (!unitId || !shotId) return
        var result = bridge.moveShotSegmentEdge(unitId, shotId, index, Math.round(toMs))
        reload()
        applyWriteResult(result, "改段边界")
    }

    // -- 四个产图按钮 ---------------------------------------------------------
    // 先置忙碌、隔一个事件循环再调用：桥槽位是同步的，一次生成要等真实服务返回，
    // 界面线程会被占住；先让按钮变灰并画出「执行中」，至少用户看得见已经开始，
    // 而不是点了半天没反应。
    function startProduce(kind) {
        if (!unitId || !selectedShotId || producing) return
        producing = true
        produceKind = kind
        produceHint = ""
        produceResult = ({})
        notice = ""
        message = ""
        produceTimer.restart()
    }

    function runProduce() {
        var kind = produceKind
        var result = kind === "layout"
            ? bridge.capturePrevisLayout(unitId, selectedShotId, frameOffset, false)
            : bridge.runGenerationTask(unitId, selectedShotId, frameOffset, kind, "")
        producing = false
        produceKind = ""
        produceResult = result || ({})
        // 失败时不重读：执行器保证失败不绑定、旧图保留，重读只会让旧图闪一下；
        // 界面因此也不会把产物状态改成「已出图」。
        if (!applyWriteResult(result, "出" + ({"layout": "框图", "sketch": "素描",
                                              "style": "风格图", "video": "视频"}[kind] || kind))) return
        reload()
        loadFrame()
    }

    function produceTip(kind) {
        if (kind === "layout") return "按这一镜绑定的预演机位，把此刻（" + seconds(frameOffset) + "）投影成框图写进去"
            + "；还没绑机位会提示去预演页绑"
        if (kind === "sketch") return "按技能声明的工作流画这一帧的素描（须未定案）；返回后刷新详情并显示产物路径"
        if (kind === "style") return "把这一帧的素描转成风格图（须已定案且该帧已有素描）"
        return "按本镜首尾风格帧生成视频（只接首尾帧模式）；没有风格帧会先报缺少输入"
    }

    // 绑定/解除预演机位：写的是 shot.previs，不动框图与时间轴，写完两处一起刷新。
    function bindPrevisCamera(index) {
        var cameras = previs.cameras || []
        if (index < 0 || index >= cameras.length) return
        var result = bridge.setShotPrevis(unitId, selectedShotId, cameras[index].camera_id,
                                          previs.scene_id || null, 0)
        applyWriteResult(result, "绑定预演机位")
        loadPrevis()
    }

    function clearPrevisBinding() {
        var result = bridge.setShotPrevis(unitId, selectedShotId, null, previs.scene_id || null, 0)
        applyWriteResult(result, "解除预演绑定")
        loadPrevis()
    }

    function previsStateText() {
        if (!previs.ok) {
            return "本场没有可用预演：" + (previs.hint || previs.error || "还没放 .director")
        }
        var binding = previsBinding || ({})
        var head = "本场预演「" + (previs.name || previs.scene_id) + "」· "
            + (previs.cameras || []).length + " 台机位"
        return binding.camera_id
            ? head + " · 本镜已绑 " + binding.camera_id + "（镜内0ms＝预演"
              + (binding.time_offset_ms || 0) + "ms）"
            : head + " · 本镜还没绑机位（出框图需要先绑一台）"
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
                 : "还没有采用视频：视频只接首尾风格帧，先用「出视频」提交一次"}
        ]
    }

    function marksFor(row) {
        var has = (row && row.has) || ({})
        return [{mark: "框", on: has.layout === true},
                {mark: "素", on: has.sketch === true},
                {mark: "风", on: has.style === true},
                {mark: "频", on: has.video === true}]
    }

    // 定位预演：预演页不接场景参数（它自己从预演工程里列场景），所以这里做两件事——
    // 把这一镜记进 app 级的选择记录（含场次名与镜号，预演页据此显示「当前定位」回执，
    // 免得用户以为点了没生效），然后切页。
    function previsSelection(shotId) {
        var id = shotId || selectedShotId
        if (!unitId || !id) return ({})
        var scene = scenes.find(s => s.unit.id === unitId) || ({})
        var row = rows.find(r => r.shot_id === id) || ({})
        return {project_path: app.project.path, screenplay_id: app.screenplayId,
                unit_id: unitId, shot_id: id, offset_ms: frameOffset,
                unit_label: scene.label || unitId,
                shot_index: row.index || 0,
                shot_title: row.title || ""}
    }

    function openPrevis(shotId) {
        var selection = previsSelection(shotId)
        if (!selection.unit_id) return
        app.storyboardSelection = selection
        app.goTo("previs")
    }

    // 产图要等真实服务，先把「执行中」画出来再进阻塞调用。
    Timer {
        id: produceTimer
        interval: 0
        repeat: false
        onTriggered: page.runProduce()
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
            page.detail = ({})
            page.frame = ({})
            page.previs = ({})
            page.forgetResults()
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
                    text: "选中左边一行，看这一镜的详情。"
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

                        // -- 镜内时间轴标尺（可拖） ---------------------------
                        Label {
                            Layout.fillWidth: true
                            text: "镜内时间轴（可拖）· 画幅 " + ((page.detail.aspect_ratio || "").length > 0
                                                                ? page.detail.aspect_ratio : "未写")
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Rectangle {
                            id: ruler
                            objectName: "shotDetailRuler"
                            Layout.fillWidth: true
                            Layout.preferredHeight: 64
                            color: page.t.panelAlt
                            border.color: page.t.line
                            radius: 4
                            clip: true

                            readonly property var timeline: page.detail.timeline || ({})
                            readonly property var points: page.detail.keyframes || []
                            readonly property var edges: page.detail.segment_edges || []
                            readonly property real span: Math.max(1, timeline.duration_ms || 1)
                            // 拖动状态只在这里：{kind, key, from_ms, value_ms, min_ms, max_ms}
                            // 拖动期间只改这个本地值，松手才调桥提交（见 finish）。
                            property var drag: null

                            function xOf(ms) { return width * Math.max(0, Math.min(span, ms)) / Math.max(1, span) }
                            function msAt(px) { return Math.round(px / Math.max(1, width) * span) }
                            function edgeValue(index, fallback) {
                                return (drag && drag.kind === "edge" && drag.key === index) ? drag.value_ms : fallback
                            }
                            function pointValue(offset, fallback) {
                                return (drag && drag.kind === "keyframe" && drag.key === offset) ? drag.value_ms : fallback
                            }
                            function begin(kind, key, fromMs, minMs, maxMs) {
                                drag = {kind: kind, key: key, from_ms: fromMs, value_ms: fromMs,
                                        min_ms: Math.min(minMs, fromMs), max_ms: Math.max(maxMs, fromMs)}
                            }
                            function moveTo(px) {
                                if (!drag) return
                                var value = Math.max(drag.min_ms, Math.min(drag.max_ms, msAt(px)))
                                drag = {kind: drag.kind, key: drag.key, from_ms: drag.from_ms,
                                        value_ms: value, min_ms: drag.min_ms, max_ms: drag.max_ms}
                            }
                            function finish() {
                                if (!drag) return
                                var state = drag
                                drag = null
                                if (state.value_ms === state.from_ms) return
                                if (state.kind === "keyframe") page.commitKeyframeDrag(page.selectedShotId, state.from_ms, state.value_ms)
                                else page.commitSegmentDrag(page.selectedShotId, state.key, state.value_ms)
                            }

                            // 段：相邻关键帧之间的一段；交替底色，节奏一眼能数。
                            // 拖动某条边界时，它两侧的两段跟着变形（同一条边界只画一个把手）。
                            Repeater {
                                model: page.detail.segments || []
                                delegate: Rectangle {
                                    required property var modelData
                                    readonly property int startMs: ruler.edgeValue(modelData.index, modelData.start_ms)
                                    readonly property int endMs: ruler.edgeValue(modelData.index + 1, modelData.end_ms)
                                    x: ruler.xOf(startMs)
                                    width: Math.max(0, ruler.xOf(endMs) - ruler.xOf(startMs))
                                    height: ruler.height
                                    color: modelData.index % 2 === 0 ? page.t.card : page.t.hover
                                    opacity: 0.9
                                }
                            }
                            // 段边界：只在内部边界画把手（整镜首尾拖开会在两端留下没人管的一段）。
                            Repeater {
                                model: ruler.edges
                                delegate: Item {
                                    required property var modelData
                                    readonly property int shownMs: ruler.edgeValue(modelData.index, modelData.at_ms)
                                    width: 11
                                    height: ruler.height
                                    x: Math.max(0, Math.min(ruler.width - width, ruler.xOf(shownMs) - width / 2))
                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 11; height: 12; radius: 2
                                        color: modelData.draggable ? page.t.handle : page.t.muted
                                        opacity: modelData.draggable ? 1 : 0.45
                                        border.width: 1
                                        border.color: page.t.line
                                    }
                                    HoverHandler { id: edgeHover }
                                    ToolTip.visible: edgeHover.hovered
                                    // 不可拖就说清为什么：点了没反应最像坏掉。
                                    ToolTip.text: modelData.draggable
                                        ? (page.seconds(shownMs) + " 的段边界：拖动会把这一刻的关键帧挪过去"
                                           + "（可落 " + page.seconds(modelData.min_ms) + "–"
                                           + page.seconds(modelData.max_ms) + "）"
                                           + (modelData.becomes_explicit ? "；拖动后分段变成显式" : ""))
                                        : modelData.reason
                                    MouseArea {
                                        objectName: "shotSegmentEdgeHandle"
                                        anchors.fill: parent
                                        preventStealing: true
                                        cursorShape: modelData.draggable ? Qt.SizeHorCursor : Qt.PointingHandCursor
                                        onPressed: {
                                            if (!modelData.draggable) {
                                                page.dragHint = modelData.reason
                                                return
                                            }
                                            // 推导段一拖就会变成显式段：按下时说一句，不弹对话框。
                                            page.dragHint = modelData.hint
                                            ruler.begin("edge", modelData.index, modelData.at_ms,
                                                        modelData.min_ms, modelData.max_ms)
                                        }
                                        onPositionChanged: if (ruler.drag) ruler.moveTo(mapToItem(ruler, mouse.x, 0).x)
                                        onReleased: ruler.finish()
                                        onCanceled: ruler.drag = null
                                    }
                                }
                            }
                            // 关键帧：竖线＋时刻；拖动改时刻，点一下只挪「此刻」。
                            Repeater {
                                model: ruler.points
                                delegate: Item {
                                    required property var modelData
                                    readonly property int shownMs: ruler.pointValue(modelData.offset_ms, modelData.offset_ms)
                                    width: 46
                                    height: ruler.height
                                    x: Math.max(0, Math.min(ruler.width - width, ruler.xOf(shownMs)))
                                    Rectangle {
                                        width: 2; height: ruler.height
                                        color: page.t.accent
                                        // 不可拖的点画淡一些，和能拖的一眼分开。
                                        opacity: modelData.draggable ? 1 : 0.45
                                    }
                                    Label {
                                        x: 3; y: 1
                                        text: page.seconds(shownMs)
                                        color: page.t.accent
                                        font.pixelSize: 9
                                        opacity: modelData.draggable ? 1 : 0.7
                                    }
                                    // 「记」＝这一刻绑了命名时刻：改时刻改的是那个标记，不是帧自己。
                                    Label {
                                        x: 3; y: ruler.height - 14
                                        visible: modelData.marker_id !== "" || modelData.has_layout
                                        text: (modelData.marker_id !== "" ? "记" : "") + (modelData.has_layout ? "框" : "")
                                        color: modelData.marker_id !== "" ? page.t.warn : page.t.ok
                                        font.pixelSize: 9
                                    }
                                    HoverHandler { id: pointHover }
                                    ToolTip.visible: pointHover.hovered
                                    ToolTip.text: modelData.draggable
                                        ? ("拖动改这一刻的时刻（" + page.seconds(modelData.offset_ms) + (modelData.moment_name ? "，" + modelData.moment_name : "")
                                           + "）；松手才提交"
                                           + (modelData.marker_id !== "" ? "；绑了命名时刻，改的是那个标记" : ""))
                                        : modelData.reason
                                    MouseArea {
                                        objectName: "shotKeyframeHandle"
                                        anchors.fill: parent
                                        preventStealing: true
                                        cursorShape: modelData.draggable ? Qt.SizeHorCursor : Qt.PointingHandCursor
                                        property real pressX: 0
                                        property int startMs: 0
                                        property bool moved: false
                                        onPressed: {
                                            pressX = mapToItem(ruler, mouse.x, 0).x
                                            startMs = modelData.offset_ms
                                            moved = false
                                            if (!modelData.draggable) page.dragHint = modelData.reason
                                        }
                                        onPositionChanged: {
                                            if (!pressed || !modelData.draggable) return
                                            var px = mapToItem(ruler, mouse.x, 0).x
                                            // 3 像素的容差：小于它算点击，不是拖动。
                                            if (!moved && Math.abs(px - pressX) < 3) return
                                            if (!moved) {
                                                moved = true
                                                ruler.begin("keyframe", startMs, startMs,
                                                            modelData.min_offset_ms, modelData.max_offset_ms)
                                            }
                                            ruler.moveTo(px)
                                        }
                                        onReleased: {
                                            if (moved) ruler.finish()
                                            else page.pickOffset(modelData.offset_ms)
                                        }
                                        onCanceled: ruler.drag = null
                                    }
                                }
                            }
                            // 声音节拍：方块；时刻要先落到尺上再画，null 的节拍不画。
                            // 本轮只显示不可拖：改节拍时刻要走声音编辑器（那里才有依据校验）。
                            Repeater {
                                model: (page.detail.sound_cues || []).filter(
                                    function (cue) { return cue.offset_ms !== null && cue.offset_ms !== undefined })
                                delegate: Rectangle {
                                    required property var modelData
                                    width: 7; height: 7
                                    y: 30
                                    x: Math.max(0, Math.min(ruler.width - width, ruler.xOf(modelData.offset_ms)))
                                    color: page.t.warn
                                    HoverHandler { id: cueHover }
                                    ToolTip.visible: cueHover.hovered
                                    ToolTip.text: (modelData.label || modelData.kind || "声音节拍") + " · "
                                        + (modelData.binding === "marker" ? "绑了命名时刻"
                                           : modelData.binding === "own" ? "用自己的时刻" : "还没有时刻")
                                        + "；" + modelData.reason
                                }
                            }
                            // 机位点：小三角位置用窄矩形代替（不引图形资源）。
                            Repeater {
                                model: page.detail.camera_keys || []
                                delegate: Rectangle {
                                    required property var modelData
                                    width: 3; height: 11
                                    y: 40
                                    x: Math.max(0, Math.min(ruler.width - width, ruler.xOf(modelData.offset_ms)))
                                    color: page.t.handle
                                    HoverHandler { id: keyHover }
                                    ToolTip.visible: keyHover.hovered
                                    ToolTip.text: "机位点 " + page.seconds(modelData.offset_ms) + "；" + modelData.reason
                                }
                            }
                            // 此刻：详情面板正在看的那一帧
                            Rectangle {
                                width: 1
                                height: ruler.height
                                visible: (page.detail.timeline || {}).duration_ms > 0
                                x: Math.max(0, Math.min(ruler.width - width, ruler.xOf(page.frameOffset)))
                                color: page.t.ink
                                opacity: 0.55
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "竖线＝关键帧（可拖，松手才提交）　把手＝段边界（可拖，吸附关键帧）"
                                + "　方块＝声音节拍（不可拖）　细条＝机位点（不可拖）　深线＝此刻"
                            color: page.t.muted
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                        Label {
                            objectName: "shotDetailDragHint"
                            Layout.fillWidth: true
                            visible: page.dragHint.length > 0
                            text: page.dragHint
                            color: page.t.warn
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                        Label {
                            objectName: "shotDetailSegmentsState"
                            Layout.fillWidth: true
                            text: (page.detail.segments_explicit === true
                                   ? "分段为显式：拖动把手会改这份显式表"
                                   : "分段为推导：拖动把手会把它变成显式分段")
                                + " · 单段上限 " + (page.detail.max_segment_ms || 0) + "ms"
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

                        // -- 预演机位：出框图的依据 ---------------------------------
                        // 出框图靠「这一镜绑了哪台预演机位」，所以绑没绑、有哪些可选，
                        // 在这里直接说清并给按钮：出框图失败时用户已经知道该做什么。
                        Label {
                            Layout.fillWidth: true
                            text: "预演机位"
                            color: page.t.ink
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Label {
                            objectName: "shotDetailPrevisState"
                            Layout.fillWidth: true
                            text: page.previsStateText()
                            color: (page.previsBinding || {}).camera_id ? page.t.body : page.t.warn
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            visible: page.previs.ok === true
                            ComboBox {
                                id: previsCamera
                                objectName: "shotDetailPrevisCamera"
                                Layout.fillWidth: true
                                implicitHeight: 24
                                font.pixelSize: 10
                                model: page.previs.cameras || []
                                textRole: "name"
                                displayText: currentIndex >= 0 && model[currentIndex]
                                    ? ((model[currentIndex].name || model[currentIndex].camera_id)
                                       + " · " + model[currentIndex].focal + "mm")
                                    : "（本场预演没有机位）"
                                currentIndex: {
                                    var binding = page.previsBinding || ({})
                                    var cameras = page.previs.cameras || []
                                    return cameras.findIndex(function (row) { return row.camera_id === binding.camera_id })
                                }
                            }
                            Button {
                                objectName: "shotDetailPrevisBind"
                                text: "绑定这一镜"
                                font.pixelSize: 10
                                implicitHeight: 24
                                enabled: !page.producing
                                onClicked: page.bindPrevisCamera(previsCamera.currentIndex)
                            }
                            Button {
                                objectName: "shotDetailPrevisUnbind"
                                text: "解除"
                                font.pixelSize: 10
                                implicitHeight: 24
                                visible: ((page.previsBinding || {}).camera_id || "").length > 0
                                onClicked: page.clearPrevisBinding()
                            }
                        }

                        // 产图类动作：四个按钮都接在真实执行器上。
                        // 执行期间全部禁用并显示「执行中」；失败只报错误，不改产物状态
                        // （旧图保留是执行器的保证，界面不该先把它标成「已出」）。
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
                                    text: page.producing && page.produceKind === modelData.key
                                        ? "执行中…" : modelData.label
                                    font.pixelSize: 10
                                    enabled: !page.producing && page.detail.ok === true && !!page.selectedShotId
                                    ToolTip.visible: hovered
                                    ToolTip.text: page.produceTip(modelData.key)
                                    onClicked: page.startProduce(modelData.key)
                                }
                            }
                        }
                        Label {
                            objectName: "shotProduceBusy"
                            Layout.fillWidth: true
                            visible: page.producing
                            text: "正在出" + page.produceLabel + "：要给真实服务跑完，返回后自动刷新详情"
                            color: page.t.muted
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            visible: page.produceHint.length > 0
                            Label {
                                objectName: "shotProduceHint"
                                Layout.fillWidth: true
                                text: page.produceHint
                                color: page.t.warn
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                            // 预演相关的原因都指向同一件要做的事：去预演页绑机位。
                            Button {
                                objectName: "shotProduceGoPrevis"
                                text: "去预演页"
                                font.pixelSize: 10
                                implicitHeight: 24
                                visible: page.produceHint.indexOf("预演") >= 0
                                onClicked: page.openPrevis(page.selectedShotId)
                            }
                        }
                        Label {
                            objectName: "shotProduceNotice"
                            Layout.fillWidth: true
                            visible: page.notice.length > 0
                            text: page.notice
                            color: page.t.ok
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                        // 产物文件：成功才可能有值（失败一律当作没有产图）。
                        Label {
                            objectName: "shotProduceOutput"
                            Layout.fillWidth: true
                            visible: (page.produceResult.output_path || "").length > 0
                            text: "产物文件：" + page.produceResult.output_path
                            color: page.t.body
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                        // 技能里这一轮没生效的项：只有这里会说，用户据此判断「为什么配了没效果」。
                        Repeater {
                            model: page.produceResult.warnings || []
                            delegate: Label {
                                required property var modelData
                                objectName: "shotProduceWarning"
                                Layout.fillWidth: true
                                text: "本轮未生效 · " + (modelData.code || "WARNING") + " · " + modelData.message
                                color: page.t.warn
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
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
