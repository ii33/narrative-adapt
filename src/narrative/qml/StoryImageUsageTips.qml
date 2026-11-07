import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: tipsDialog
    objectName: "storyboardImageUsageTipsDialog"
    required property var app
    readonly property var t: app.theme
    property var tips: ({})
    property bool loading: false
    property string errorText: ""
    property int requestToken: 0
    property string requestProject: ""
    readonly property string contentText: {
        const parts = []
        if (tips.summary) parts.push(tips.summary)
        if (tips.current_workflow) parts.push("当前软件支持\n" + tips.current_workflow)
        for (const section of tips.sections || []) {
            parts.push(section.title + "\n" + (section.paragraphs || []).join("\n\n"))
        }
        if (tips.scope) parts.push("适用范围\n" + tips.scope)
        return parts.join("\n\n")
    }

    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.max(1, Math.min(760, app.width - 24))
    height: Math.max(1, Math.min(650, app.height - 24))
    modal: true
    title: tips.title || "生图技巧"
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    function showTips() {
        requestToken = app.nextStoryboardToken()
        requestProject = app.project.path || ""
        tips = ({})
        errorText = ""
        loading = true
        open()
        bridge.storyboardAction("read_storyboard_usage_tips", {}, requestToken)
    }

    onClosed: {
        requestToken = 0
        loading = false
    }

    Connections {
        target: app
        function onProjectChanged() {
            if (tipsDialog.requestProject !== (tipsDialog.app.project.path || "")) {
                tipsDialog.close()
                tipsDialog.requestToken = 0
                tipsDialog.loading = false
                tipsDialog.tips = ({})
                tipsDialog.errorText = ""
                tipsDialog.requestProject = ""
            }
        }
    }
    Connections {
        target: bridge
        function onResultReady(kind, payload) {
            if (kind !== "storyboard" || payload.operation !== "read_storyboard_usage_tips"
                    || !tipsDialog.requestToken || payload.token !== tipsDialog.requestToken
                    || tipsDialog.requestProject !== (tipsDialog.app.project.path || "")) return
            tipsDialog.loading = false
            tipsDialog.errorText = payload.error || ""
            tipsDialog.tips = payload.error ? ({}) : (payload.result || {})
        }
    }

    contentItem: ColumnLayout {
        spacing: 10
        Label {
            objectName: "storyboardImageUsageTipsLoading"
            visible: tipsDialog.loading
            text: "正在读取使用技巧…"
            color: tipsDialog.t.muted
        }
        Label {
            objectName: "storyboardImageUsageTipsError"
            visible: !!tipsDialog.errorText
            text: tipsDialog.errorText
            color: tipsDialog.t.warn
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
        ScrollView {
            id: tipsScroll
            objectName: "storyboardImageUsageTipsScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            TextArea {
                id: tipsText
                objectName: "storyboardImageUsageTipsText"
                width: tipsScroll.availableWidth
                text: tipsDialog.contentText
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                selectByKeyboard: true
                persistentSelection: true
                wrapMode: TextEdit.Wrap
                color: tipsDialog.t.ink
                padding: 12
            }
        }
    }
    footer: DialogButtonBox {
        standardButtons: DialogButtonBox.Close
        onRejected: tipsDialog.close()
        Button {
            objectName: "storyboardImageUsageTipsReload"
            text: "重新读取"
            enabled: !tipsDialog.loading
            DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
            onClicked: tipsDialog.showTips()
        }
        Button {
            objectName: "storyboardImageUsageTipsCopy"
            text: "复制全部"
            enabled: !!tipsDialog.contentText
            DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
            onClicked: {
                tipsText.selectAll()
                tipsText.copy()
            }
        }
    }
}
