"""New events require an explicit location selection; ordinary edits preserve it."""
from PySide6.QtCore import Q_ARG, QMetaObject
from test_chat_scroll import transcript_shell
from test_screenplay_ui import dialog_action, render_ready
from test_static_check_ui import click, plain

from narrative.operations import Operations


def choose(control, value):
    rows = plain(control.property('model'))
    control.setProperty('currentIndex', next(i for i, row in enumerate(rows) if row['id'] == value))


def assert_location(snapshot, event_id, value):
    row = next(p for p in snapshot['properties'] if p.get('node_id') == event_id and p.get('event_id') == event_id)
    assert row['scope'] == 'world'
    if value == '未知':
        assert row['op'] == 'unknown' and 'value' not in row
    else:
        assert row['op'] == 'set' and row['value'] == value


def test_adaptation_event_location_explicit_choices_staged_restore_and_edit(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QUICK_CONTROLS_STYLE', 'Fusion')
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        scene = ops.edit_adaptation_node(changes={'node_type':'scene','name':'桥边'})['created_node_id']
        place = ops.edit_adaptation_node(changes={'node_type':'location','name':'桥头'})['created_node_id']
        hidden = ops.edit_adaptation_node(changes={'node_type':'location','name':'屏蔽地点'})['created_node_id']
        ops.suppress_adaptation_node(hidden)
        ui.window.setProperty('assistantOpen', False)
        ui.window.setProperty('currentPage', 'adaptation')
        page = ui.window.findChild(object, 'adaptationBrowsePage')
        ui.wait(lambda: len(plain(page.property('allAdaptationNodes'))) == 3)
        editor = page.findChild(object, 'nodeEditor')
        for index, value in enumerate((place, '无', '未知')):
            click(ui, page.findChild(object, 'adaptationNewNode'))
            ui.wait(lambda: editor.property('opened'))
            editor.findChild(object, 'editorType').setProperty('currentIndex', 4)
            location = editor.findChild(object, 'editorInitialLocation')
            assert location.property('currentIndex') == 0
            assert hidden not in [r['id'] for r in plain(location.property('model'))]
            assert not editor.findChild(object, 'editorSubmitNode').property('enabled')
            choose(editor.findChild(object, 'editorParent'), scene)
            choose(location, value)
            if index == 0:
                # Empty name is a content error; the selected location survives staging.
                click(ui, dialog_action(ui, page, 'nodeEditor', 'editorSubmitNode'))
                ui.wait(lambda: any(k == 'content_submission' for k, _ in ui.results))
                assert editor.property('visible') and plain(location.property('model'))[location.property('currentIndex')]['id'] == place
                editor.close()
                ui.wait(lambda: not editor.property('visible'))
                click(ui, page.findChild(object, 'adaptationNewNode'))
                ui.wait(lambda: editor.property('opened'))
                restore = editor.findChild(object, 'contentSubmissionRestore')
                ui.wait(lambda restore=restore: restore.property('visible'))
                click(ui, restore)
                ui.wait(lambda: editor.findChild(object, 'editorType').property('currentIndex') == 4)
                location = editor.findChild(object, 'editorInitialLocation')
                assert plain(location.property('model'))[location.property('currentIndex')]['id'] == place
            editor.findChild(object, 'editorName').setProperty('text', '发生'+str(index))
            click(ui, dialog_action(ui, page, 'nodeEditor', 'editorSubmitNode'))
            ui.wait(lambda: not editor.property('visible'))
            event = next(n for n in ops.adaptation_snapshot()['nodes'] if n['name'] == '发生'+str(index))
            assert_location(ops.adaptation_snapshot(), event['id'], value)
        before = ops.adaptation_snapshot()['properties']
        QMetaObject.invokeMethod(editor, 'openFor', Q_ARG('QVariant', event['id']))
        ui.wait(lambda: editor.property('opened') and plain(editor.property('node')).get('id') == event['id'])
        editor.findChild(object, 'editorName').setProperty('text', '只改名')
        click(ui, dialog_action(ui, page, 'nodeEditor', 'editorSubmitNode'))
        ui.wait(lambda: any(n['name'] == '只改名' for n in ops.adaptation_snapshot()['nodes']))
        assert ops.adaptation_snapshot()['properties'] == before
        assert not ui.errors


def test_script_event_location_choices_and_existing_edit_preserve_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QUICK_CONTROLS_STYLE', 'Fusion')
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        sid = ops.create_screenplay('地点选择', reason='隔离UI')['screenplay']['id']
        scene = ops.edit_screenplay_timeline_node(sid, changes={'node_type':'scene','name':'桥边'}, reason='隔离场景', expected_timeline_revision=0)['created_node_id']
        place = ops.edit_screenplay_timeline_node(sid, changes={'node_type':'location','name':'桥头'}, reason='隔离地点', expected_timeline_revision=1)['created_node_id']
        staged = ops.edit_screenplay_timeline_node(sid, changes={'node_type':'event','name':'','initial_location':place}, parent_id=scene, expected_timeline_revision=2, reason='待修名称fixture')
        assert staged['status'] == 'staged'
        ui.window.setProperty('assistantOpen', False)
        ui.window.setProperty('currentPage', 'screenplay')
        page = ui.window.findChild(object, 'screenplayPage')
        QMetaObject.invokeMethod(page, 'selectScreenplay', Q_ARG('QVariant', sid))
        ui.wait(lambda: plain(page.property('screenplay')).get('id') == sid)
        editor = page.findChild(object, 'scriptTimelineEditor')
        for index, value in enumerate((place, '无', '未知')):
            ui.wait(lambda: not page.property('timelineBusy') and not ui.window.property('busy'))
            render_ready(ui)
            click(ui, page.findChild(object, 'scriptTimelineNew'))
            ui.wait(lambda: editor.property('opened'))
            editor.findChild(object, 'scriptTimelineNodeType').setProperty('currentIndex', 3)
            location = editor.findChild(object, 'scriptTimelineInitialLocation')
            assert location.property('currentIndex') == 0
            assert not editor.findChild(object, 'scriptTimelineNodeSave').property('enabled')
            choose(editor.findChild(object, 'scriptTimelineNodeParent'), scene)
            choose(location, value)
            if index == 0:
                restore = editor.findChild(object, 'contentSubmissionRestore')
                ui.wait(lambda restore=restore: restore.property('visible'))
                click(ui, restore)
                ui.wait(lambda: editor.findChild(object, 'scriptTimelineNodeType').property('currentIndex') == 3)
                assert plain(location.property('model'))[location.property('currentIndex')]['id'] == place
            editor.findChild(object, 'scriptTimelineNodeName').setProperty('text', '事件'+str(index))
            ui.results.clear()
            click(ui, dialog_action(ui, page, 'scriptTimelineEditor', 'scriptTimelineNodeSave'))
            ui.wait(lambda: not editor.property('visible') or any(k == 'content_submission' for k, _ in ui.results))
            assert not editor.property('visible'), [(k,p) for k,p in ui.results if k == 'content_submission']
            ui.wait(lambda: not page.property('timelineBusy') and not ui.window.property('busy'))
            event = next(n for n in ops.read_screenplay_timeline(sid)['nodes'] if n['name'] == '事件'+str(index))
            assert_location(ops.read_screenplay_timeline(sid), event['id'], value)
        assert ops.read_content_submission(staged['submission_id'])['status'] == 'accepted'
        before = ops.read_screenplay_timeline(sid)['properties']
        render_ready(ui)
        page.findChild(object, 'scriptRangePicker').setProperty('selectedId', event['id'])
        click(ui, page.findChild(object, 'scriptTimelineEdit'))
        ui.wait(lambda: editor.property('opened'))
        editor.findChild(object, 'scriptTimelineNodeName').setProperty('text', '只改名')
        click(ui, dialog_action(ui, page, 'scriptTimelineEditor', 'scriptTimelineNodeSave'))
        ui.wait(lambda: not editor.property('visible'))
        assert ops.read_screenplay_timeline(sid)['properties'] == before
        assert not ui.errors
