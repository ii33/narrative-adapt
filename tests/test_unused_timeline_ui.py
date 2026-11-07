"""Actual Qt timeline dims only reliably unused events after a completed task."""
import os
import subprocess
import sys
from pathlib import Path


def test_completed_usage_dimming_keeps_nodes_selectable_and_scope_safe(tmp_path):
    qml_dir = (Path(__file__).resolve().parents[1] / 'src/narrative/qml').as_uri()
    wrapper = tmp_path / 'usage-picker.qml'
    wrapper.write_text('''import QtQuick
import QtQuick.Controls
import "''' + qml_dir + '''"
ApplicationWindow {
    width:700;height:500;visible:true
    property alias picker: picker
    ScriptRangePicker {
        id:picker;anchors.fill:parent;timelineId:"timeline-one"
        t:({ink:"#111111",body:"#222222",muted:"#888888"})
        nodes:[{id:"E1",node_type:"event",name:"使用事件",sort_id:1},
               {id:"E2",node_type:"event",name:"未使用事件",sort_id:2},
               {id:"E3",node_type:"event",name:"屏蔽事件",sort_id:3,suppressed:true},
               {id:"S1",node_type:"scene",name:"场景",sort_id:4}]
    }
}
''', encoding='utf-8')
    code = '''import sys
from PySide6.QtCore import QUrl,QMetaObject,Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQml import QQmlEngine,QQmlComponent,QQmlExpression
from PySide6.QtTest import QTest
app=QGuiApplication([]);engine=QQmlEngine()
component=QQmlComponent(engine,QUrl.fromLocalFile(sys.argv[1]));root=component.create()
assert root is not None,component.errors()
def evaluate(expression):
 e=QQmlExpression(QQmlEngine.contextForObject(root),root,expression);value=e.evaluate()[0]
 assert not e.hasError(),e.error().toString()
 return value.toVariant() if hasattr(value,'toVariant') else value
QTest.qWait(100)
def walk(item):
 yield item
 for child in item.childItems():yield from walk(child)
def node(identifier):
 values=[item for item in walk(root.contentItem()) if item.objectName()=='scriptRangeNode' and item.property('nodeId')==identifier]
 assert len(values)==1,(identifier,len(values))
 return values[0]
def label(identifier):return node(identifier).property('contentItem')
def set_usage(status,known=True,timeline='timeline-one'):
 evaluate("picker.eventUsage={status:%r,known:%s,timeline_id:%r,unused_event_ids:['E2','E3','S1'],unknown_event_ids:%s,used_event_ids:['E1']}"%(status,'true' if known else 'false',timeline,'[]' if known else "['E2']"));app.processEvents()
for status in ('not_generated','running','cancelled','failed'):
 set_usage(status)
 assert label('E2').property('opacity')==1,status
set_usage('completed',False)
assert label('E2').property('opacity')==1
evaluate("picker.eventUsage={status:'completed',known:false,timeline_id:'timeline-one',unused_event_ids:['E2'],unknown_event_ids:['E1'],used_event_ids:[]}")
app.processEvents()
assert label('E2').property('opacity')<1
assert label('E1').property('opacity')==1
set_usage('completed',True,'timeline-other')
assert label('E2').property('opacity')==1
set_usage('completed')
assert node('E2').property('unusedEvent') is True
assert 0<label('E2').property('opacity')<1
assert label('E1').property('opacity')==1
assert label('E3').property('opacity')==1
assert label('S1').property('opacity')==1
assert node('E2').property('enabled') is True
assert QMetaObject.invokeMethod(node('E2'),'clicked',Qt.DirectConnection)
app.processEvents()
assert evaluate('picker.selectedId')=='E2'
assert label('E2').property('opacity')==1
evaluate("picker.selectedId='E1'");app.processEvents()
assert label('E2').property('opacity')<1
start=[i for i in walk(root.contentItem()) if i.objectName()=='scriptRangeStart' and i.property('nodeId')=='E2'][0]
assert QMetaObject.invokeMethod(start,'clicked',Qt.DirectConnection)
assert evaluate('picker.startId')=='E2'
set_usage('running')
assert label('E2').property('opacity')==1
root.close()
'''
    result = subprocess.run([sys.executable, '-c', code, str(wrapper)],
        env=os.environ | {'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software'},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_workspace_refreshes_usage_once_for_generic_task_start(tmp_path):
    qml_dir = (Path(__file__).resolve().parents[1] / 'src/narrative/qml').as_uri()
    wrapper = tmp_path / 'usage-workspace.qml'
    wrapper.write_text('''import QtQuick
import QtQuick.Controls
import "''' + qml_dir + '''"
ApplicationWindow {
    width:1400;height:900;visible:true
    property alias page: page
    QtObject {
        id:appState
        property string mode:"launcher"
        property string screenplayId:"screenplay-one"
        property var screenplaySelection:({})
        property var project:({path:"isolated.nproj"})
        property var theme:({ink:"#111",body:"#222",muted:"#888",warn:"#a60",bad:"#a00",card:"#fff",line:"#ddd",accent:"#25f",ok:"#080"})
        property bool busy:false
        property bool assistantOpen:false
        function toast(message,error){}
    }
    ScreenplayPage {id:page;anchors.fill:parent;visible:false;app:appState}
}
''', encoding='utf-8')
    code = '''import sys
from PySide6.QtCore import QUrl,QObject,Signal,Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQml import QQmlEngine,QQmlComponent,QQmlExpression
class Bridge(QObject):
 resultReady=Signal(str,'QVariantMap')
 errorOccurred=Signal(str,str)
 def __init__(self):super().__init__();self.loads=[]
 @Slot(str)
 def loadScreenplayWorkspace(self,identifier):self.loads.append(identifier)
 @Slot()
 def loadPromptSettings(self):pass
 @Slot(result=str)
 def newContentFormSession(self):return 'form-test'
 @Slot(str,bool,int)
 def activateContentForm(self,*args):pass
 @Slot(str,str,str)
 def listContentSubmissions(self,*args):pass
app=QGuiApplication([]);engine=QQmlEngine();bridge=Bridge();engine.rootContext().setContextProperty('bridge',bridge)
component=QQmlComponent(engine,QUrl.fromLocalFile(sys.argv[1]));root=component.create()
assert root is not None,component.errors()
def evaluate(expression):
 e=QQmlExpression(QQmlEngine.contextForObject(root),root,expression);value=e.evaluate()[0]
 assert not e.hasError(),e.error().toString()
 return value.toVariant() if hasattr(value,'toVariant') else value
evaluate("page.app.mode='workspace';page.visible=true")
app.processEvents();before=len(bridge.loads)
evaluate("page.workspace={document:{screenplay:{id:'screenplay-one',timeline_id:'timeline-one'},event_usage:{status:'completed',timeline_id:'timeline-one',unused_event_ids:['E2']}},timeline:{nodes:[{id:'E2',node_type:'event',name:'unused',sort_id:1}]}}")
app.processEvents()
def walk(item):
 yield item
 for child in item.childItems():yield from walk(child)
def opacity():
 rows=[i for i in walk(root.contentItem()) if i.objectName()=='scriptRangeNode' and i.property('nodeId')=='E2']
 assert len(rows)==1,len(rows)
 return rows[0].property('contentItem').property('opacity')
def emit(kind,payload):bridge.resultReady.emit(kind,payload);app.processEvents()
assert opacity()<1
emit('conversation_progress',{'task_id':'plain-task','event':'assistant_started','busy':True})
assert len(bridge.loads)==before+1
assert opacity()==1  # audit still contains the previous successful task
emit('conversation_progress',{'task_id':'plain-task','status':'running','busy':True})
assert len(bridge.loads)==before+1
emit('conversation_progress',{'task_id':'plain-task','event':'tool_complete','busy':True})
assert len(bridge.loads)==before+2  # audit now exists: refresh once more
emit('conversation_progress',{'task_id':'plain-task','event':'tool_complete','busy':True})
assert len(bridge.loads)==before+2
emit('conversation_progress',{'task_id':'next-task','status':'running','busy':True})
assert len(bridge.loads)==before+3
emit('conversation_progress',{'task_id':'next-task','event':'task_complete','busy':False})
assert len(bridge.loads)==before+4
assert opacity()==1
# A response completing another task's refresh cannot clear the first active task.
evaluate("page.usageReadFinished('screenplay-one')")
assert opacity()==1
emit('assistant',{'task_id':'plain-task'})
assert opacity()==1  # wait for authoritative workspace response
payload=evaluate('page.workspace')
emit('screenplay_workspace',payload)
assert opacity()<1
# A stale tool notification cannot reactivate a finished task.
emit('conversation_progress',{'task_id':'plain-task','event':'tool_complete','busy':True})
assert opacity()<1
emit('screenplay_writing',{'task_id':'scoped-task','target':{'screenplay_id':'screenplay-other'}})
assert opacity()<1
emit('conversation_progress',{'task_id':'stale-stop','event':'stop_ignored','busy':True})
assert opacity()<1
evaluate("page.app.screenplayId='screenplay-other'")
assert evaluate('page.usageBusy') is True
evaluate("page.app.project={path:'new-isolated.nproj'}")
assert evaluate('page.usageBusy') is False
emit('conversation_progress',{'task_id':'old-project-task','project_path':'isolated.nproj','busy':True})
assert evaluate('page.usageBusy') is False
root.close()
'''
    result = subprocess.run([sys.executable, '-c', code, str(wrapper)],
        env=os.environ | {'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software'},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
