"""Real form feedback keeps invalid authored content editable and patches the same draft."""
from test_chat_scroll import transcript_shell
from test_screenplay_ui import create_fixture, dialog_action, render_ready
from test_static_check_ui import click, plain


def click_save(ui, page, editor):
    """Reveal and click the real form action after staged problems widened the form."""
    from PySide6.QtCore import QPointF

    button = dialog_action(ui, page, 'scriptPlanEditor', 'scriptPlanSave')
    scroll = button.parentItem()
    while scroll is not None and scroll.metaObject().indexOfProperty('contentX') < 0:
        scroll = scroll.parentItem()
    assert scroll is not None
    scroll.setProperty('contentX', max(0, scroll.property('contentWidth') - scroll.property('width')))
    render_ready(ui)
    assert 0 <= button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).x() <= ui.window.width()
    click(ui, button)


def test_scene_plan_staged_fix_and_explicit_reopen_restore(tmp_path, monkeypatch):
    from PySide6.QtCore import Q_ARG, QMetaObject

    from narrative.operations import Operations

    monkeypatch.setenv('QT_QUICK_CONTROLS_STYLE', 'Fusion')
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        sid = create_fixture(ops)
        ui.window.setProperty('assistantOpen', False)
        ui.window.setProperty('currentPage', 'screenplay')
        page = ui.window.findChild(object, 'screenplayPage')
        QMetaObject.invokeMethod(page, 'selectScreenplay', Q_ARG('QVariant', sid))
        ui.wait(lambda: plain(page.property('segment')).get('id'))
        render_ready(ui)
        click(ui, page.findChild(object, 'scriptAddUnit'))
        editor = page.findChild(object, 'scriptPlanEditor')
        ui.wait(lambda: editor.property('opened'))
        editor.findChild(object, 'scriptPlanSummary').setProperty('text', '甲走过桥。')
        editor.findChild(object, 'scriptPlanLocationId').setProperty('text', 'missing-place')
        click(ui, dialog_action(ui, page, 'scriptPlanEditor', 'scriptPlanSave'))
        ui.wait(lambda: any(kind == 'content_submission' for kind, _ in ui.results))
        staged = next(p for k, p in reversed(ui.results) if k == 'content_submission')
        assert editor.property('visible')
        assert not plain(page.property('units'))
        assert editor.findChild(object, 'contentSubmissionProblems').property('text')
        # The staged insert reports the original authored field path, not a whole-form error.
        assert any(problem['path'] == ['changes', 0, 'unit', 'plan', 'location_id']
                   for problem in staged['problems'])
        # Closing does not discard the durable draft; reopening offers explicit restore.
        QMetaObject.invokeMethod(editor, 'close')
        ui.wait(lambda: not editor.property('visible'))
        click(ui, page.findChild(object, 'scriptAddUnit'))
        ui.wait(lambda: editor.property('opened'))
        restore = editor.findChild(object, 'contentSubmissionRestore')
        ui.wait(lambda: restore.property('visible'))
        click(ui, restore)
        ui.wait(lambda: editor.findChild(object, 'scriptPlanLocationId').property('text') == 'missing-place')
        editor.findChild(object, 'scriptPlanLocationId').setProperty('text', '')
        click_save(ui, page, editor)
        ui.wait(lambda: not editor.property('visible'))
        ui.wait(lambda: len(plain(page.property('units'))) == 1)
        saved = ops.read_content_submission(staged['submission_id'])
        assert saved['status'] == 'accepted'
        assert len(ops.read_script_segment(page.property('selectedSegmentId'))['units']) == 1
        assert not ui.errors
