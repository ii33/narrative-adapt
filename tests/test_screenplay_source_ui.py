"""The actual plan editor exposes named timeline sources and saves selections."""
import os
import subprocess
import sys
from pathlib import Path


def test_plan_editor_named_source_selection_and_save(tmp_path):
    qml_dir = (Path(__file__).resolve().parents[1] / 'src/narrative/qml').as_uri()
    wrapper = tmp_path / 'source-editor.qml'
    wrapper.write_text('''import QtQuick
import QtQuick.Controls
import "''' + qml_dir + '''"
ApplicationWindow {
    id: host; width: 1000; height: 900; visible: true
    property var t: ({ink:"#111111",muted:"#666666",warn:"#996600",bad:"#aa0000"})
    property var segment: ({id:"segment-1",span:{input:{source_event_ids:["E1","E2"]}}})
    property var workspace: ({timeline:{nodes:[{id:"E1",name:"等候"},{id:"E2",name:"到达"}]}})
    property string selectedUnitId: "unit-1"
    property var lastChange: ({})
    property alias sourceEditor: editor
    function changeSceneTable(change){lastChange=change}
    ScriptPlanEditor {id: editor; host: host}
}
''', encoding='utf-8')
    code = '''import sys
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlEngine, QQmlComponent, QQmlExpression
app=QGuiApplication([]);engine=QQmlEngine()
component=QQmlComponent(engine,QUrl.fromLocalFile(sys.argv[1]));root=component.create()
assert root is not None,component.errors()
def evaluate(expression):
 e=QQmlExpression(QQmlEngine.contextForObject(root),root,expression);value=e.evaluate()[0]
 assert not e.hasError(),e.error().toString()
 return value.toVariant() if hasattr(value,'toVariant') else value
evaluate("sourceEditor.openFor({id:'unit-1',kind:'scene',enabled:true,plan:{title:'会合',summary:'双方会合',source_event_ids:['E1']}})")
app.processEvents()
options=evaluate('sourceEditor.sourceOptions')
assert [(v['id'],v['name']) for v in options]==[('E1','等候'),('E2','到达')],options
evaluate("sourceEditor.selectSource('E2',true)")
evaluate('sourceEditor.save()')
value=evaluate('lastChange')
assert value['changes']['plan']['source_event_ids']==['E1','E2'],value
assert value['unit_id']=='unit-1'
evaluate("sourceEditor.selectSource('E1',false)")
evaluate("sourceEditor.selectSource('E2',false)")
evaluate('sourceEditor.save()')
assert evaluate('lastChange')['changes']['plan']['source_event_ids']==[]
root.close()
'''
    result = subprocess.run([sys.executable, '-c', code, str(wrapper)],
        env=os.environ | {'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software'},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
