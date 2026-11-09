import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: feedback
    required property var state
    // 这个组件被二十多处表单和弹层复用，宿主不一定传主题进来：直接读引擎的
    // bridge（弹层里也能用），上下文属性还没注入时用浅色兜底，避免未定义颜色。
    readonly property var t: (bridge && bridge.theme) ? bridge.theme : ({warn: "#b06a00", muted: "#6d7f96"})
    Layout.fillWidth:true
    visible:!!state.submission.submission_id||state.candidates.length>0||!!state.errorText
    spacing:4
    Label{objectName:"contentSubmissionStatus";Layout.fillWidth:true;wrapMode:Text.Wrap;text:feedback.state.submission.submission_id?"内容已暂存，尚未入库 · "+feedback.state.submission.submission_id+" · 修订 "+feedback.state.submission.submission_revision:feedback.state.errorText?"内容尚未保存。":"此表单有未完成的暂存内容。";color:t.warn;font.pixelSize:11}
    Label{objectName:"contentSubmissionProblems";Layout.fillWidth:true;wrapMode:Text.Wrap;text:feedback.state.problemText()+(feedback.state.errorText?"\n"+feedback.state.errorText:"");visible:!!text;font.pixelSize:11;color:t.warn}
    Repeater{model:feedback.state.submission.problems||[]
        Button{required property var modelData;visible:(modelData.path||[]).length>0;flat:true;text:"定位问题字段 · "+(modelData.path||[]).join(" / ");onClicked:feedback.state.focusProblem(modelData)}
    }
    Label{visible:!!feedback.state.submission.submission_id;Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:10;text:"修改问题字段后再次保存，将修补同一份暂存；校验通过后自动入库。";color:t.muted}
    RowLayout{visible:!feedback.state.submission.submission_id&&feedback.state.candidates.length>0;Layout.fillWidth:true
        ComboBox{id:choice;Layout.fillWidth:true;model:feedback.state.candidates;textRole:"submission_id"}
        Button{objectName:"contentSubmissionRestore";text:"恢复暂存";onClicked:feedback.state.restore(choice.currentIndex)}
    }
}
