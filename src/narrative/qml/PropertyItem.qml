import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 一条属性：当前值 + 它披露在哪一次。历史披露不再铺在浏览区（要看改动序列去节点编辑器）。
// 分类只影响显示位置：点分类标签可以把它移到另一个大类，取值本身不动。
Column {
    id: item

    required property var row        // BrowsePage 组装好的行
    required property var host       // 所属页面，负责跳转与移动分类

    readonly property var t: host.t
    // 同一属性可能分在两个大类里，这时说明清楚，避免看起来像少了几条。
    readonly property string splitHint: (row.otherCategories || []).length > 0
        ? "也在「" + (row.otherCategories || []).join("」「") + "」中" : ""

    spacing: 0

    Rectangle {
        id: head
        width: parent.width
        // 取值列表逐项一行时这一行会变高；46 只是一行时的最小高度。
        height: Math.max(46, headContent.implicitHeight + 8)
        radius: 4
        color: headHover.hovered ? item.t.accentSoft : "transparent"

        HoverHandler { id: headHover }

        // 先声明，后声明的标签才能抢到点击（分类标签、持有者各有自己的目标）。
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: item.host.toggleProperty(item.row.key)
        }

        ColumnLayout {
            id: headContent
            anchors.fill: parent
            anchors.leftMargin: 8
            anchors.rightMargin: 8
            anchors.topMargin: 4
            anchors.bottomMargin: 4
            spacing: 0

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Label {
                    // 只显示当前值：历史披露不再铺在浏览区（要看改动去节点编辑器）。
                    text: item.row.label
                    color: item.t.ink
                    font.pixelSize: 12
                    Layout.preferredWidth: 92
                    elide: Text.ElideRight
                    Layout.minimumWidth: 0
                }
                Label {
                    // 取值是列表，逐项一行：这里换行显示，不再按单行省略。
                    text: item.row.valueText
                    color: item.row.valueNodeId.length > 0 ? item.t.accent : item.t.body
                    font.pixelSize: 12
                    font.underline: item.row.valueNodeId.length > 0
                    wrapMode: Text.Wrap
                    Layout.minimumWidth: 0
                    Layout.fillWidth: true

                    MouseArea {
                        anchors.fill: parent
                        enabled: item.row.valueNodeId.length > 0
                        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                        onClicked: item.host.jumpTo(item.row.valueNodeId)
                    }
                }
                Label {
                    text: item.row.scopeText
                    color: item.t.accent
                    font.pixelSize: 10
                    // 多作用域时会写成「声称/认知/世界」，不限宽就会把分类标签挤出面板。
                    Layout.maximumWidth: 92
                    elide: Text.ElideRight
                    Layout.minimumWidth: 0
                }
                Label {
                    visible: item.row.holderId.length > 0
                    text: item.row.holderName
                    color: item.t.accent
                    font.pixelSize: 10
                    font.underline: true
                    Layout.maximumWidth: 72
                    elide: Text.ElideRight
                    Layout.minimumWidth: 0
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: item.host.jumpTo(item.row.holderId)
                    }
                }
                // 整条属性一起移：同一属性下的记录通常属于同一个方面。
                Rectangle {
                    objectName: "propertyCategoryChip_" + item.row.key
                    implicitWidth: categoryLabel.implicitWidth + 12
                    implicitHeight: 16
                    radius: 8
                    color: categoryArea.containsMouse ? item.t.accent : item.t.accentSoft
                    Label {
                        id: categoryLabel
                        anchors.centerIn: parent
                        text: item.row.category + (item.row.categorySource === "fallback" ? " ?" : "")
                        color: categoryArea.containsMouse ? item.t.onAccent : item.t.accent
                        font.pixelSize: 10
                    }
                    MouseArea {
                        id: categoryArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: item.host.askCategory(item.row.category, item.row.recordIds)
                    }
                }
                // 两个名字合成一个方面：只在真合了的时候给一个入口，不单独占一行。
                Label {
                    visible: item.row.nameIds.length > 1
                    text: "合并…"
                    color: item.t.accent
                    font.pixelSize: 10
                    font.underline: true
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: item.host.askMerge(item.row.nameIds)
                    }
                }
            }

            Label {
                Layout.fillWidth: true
                visible: item.row.disclosedText.length > 0 || item.splitHint.length > 0
                    || (item.row.derivedText || "").length > 0
                // 只说这句是哪一次披露的。失效的不在这里出现，所以也不提。
                text: [item.row.disclosedText, item.row.derivedText || "", item.splitHint]
                    .filter(function (part) { return part.length > 0 }).join(" · ")
                color: item.t.muted
                font.pixelSize: 10
                elide: Text.ElideRight
                Layout.minimumWidth: 0
            }
        }
    }

}
