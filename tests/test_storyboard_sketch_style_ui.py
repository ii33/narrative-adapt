"""Panel and keyframe drawing share the project sketch style and color choice."""
from test_chat_scroll import transcript_shell
from test_screenplay_ui import dialog_action, render_ready
from test_static_check_ui import click, plain
from test_storyboard_asset_elements_ui import open_desk, seed


def test_panel_and_keyframe_requests_share_simple_color_without_style_render(tmp_path, monkeypatch):
    from PySide6.QtCore import QMetaObject

    from narrative.graph_operations import GraphOperations
    from narrative.operations import Operations
    calls = []

    def sketch(self, unit_id, shot_id, offset_ms, layout, expected_revision, prompt="", simple_color=False):
        calls.append(("keyframe", simple_color))
        return self.read_storyboard(unit_id)

    def panel(self, unit_id, shot_id, panel_id, expected_revision, prompt="", simple_color=False):
        calls.append(("panel", simple_color))
        return self.read_storyboard(unit_id)

    def panels(self, unit_id, shot_id, expected_revision, panel_ids=None, missing_only=True, simple_color=False):
        calls.append(("missing" if missing_only else "batch", simple_color))
        return self.read_storyboard(unit_id)

    monkeypatch.setattr(GraphOperations, "generate_storyboard_sketch", sketch)
    monkeypatch.setattr(GraphOperations, "generate_storyboard_panel", panel)
    monkeypatch.setattr(GraphOperations, "generate_storyboard_panels", panels)
    monkeypatch.setattr(GraphOperations, "render_storyboard_style", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not render project style")))
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        ops.apply_storyboard_changes(unit, board["revision"], [{"op": "create_panel", "shot_id": board["shots"][0]["id"], "offset_ms": 0, "intent": "甲停步"}])
        desk = open_desk(ui, screenplay)
        assert not desk.property("sketchSimpleColor") and desk.property("viewMode") == 0
        click(ui, desk.findChild(object, "storyboardPanelMode"))
        render_ready(ui)
        color = desk.findChild(object, "storyboardPanelSimpleColor")
        assert "描述型素描" in desk.findChild(object, "storyboardPanelSketchStyleHint").property("text")
        click(ui, color)
        assert desk.property("sketchSimpleColor")
        click(ui, desk.findChild(object, "storyboardGenerateSinglePanel"))
        ui.wait(lambda: len(calls) == 1 and not desk.property("busy"))
        assert calls[-1] == ("panel", True)
        click(ui, color)
        click(ui, desk.findChild(object, "storyboardGenerateMissingPanels"))
        ui.wait(lambda: len(calls) == 2 and not desk.property("busy"))
        assert calls[-1] == ("missing", False)
        click(ui, color)
        click(ui, desk.findChild(object, "storyboardRegeneratePanels"))
        ui.wait(lambda: len(calls) == 3 and not desk.property("busy"))
        assert calls[-1] == ("batch", True)
        QMetaObject.invokeMethod(desk, "editScriptFrame")
        dialog = desk.findChild(object, "storyboardFrameDialog")
        ui.wait(lambda: dialog.property("opened"))
        assert desk.findChild(object, "storyboardSketchSimpleColor").property("checked")
        assert "描述型素描" in desk.findChild(object, "storyboardSketchStyleHint").property("text")
        click(ui, dialog_action(ui, desk, "storyboardFrameDialog", "storyboardSketch"))
        ui.wait(lambda: len(calls) == 4 and not desk.property("busy"))
        assert calls[-1] == ("keyframe", True)
        assert desk.property("viewMode") == 0
        assert not ui.errors


def test_project_settings_exposes_editable_shared_sketch_style(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        ui.window.setProperty("assistantOpen", False)
        ui.window.setProperty("currentPage", "project_settings")
        page = ui.window.findChild(object, "projectSettingsPage")
        ui.wait(lambda: page.property("promptsLoaded") and not ui.window.property("busy"))
        rows = plain(page.property("promptSettings"))["prompts"]
        index = next(i for i, row in enumerate(rows) if row["id"] == "sketch_style")
        page.findChild(object, "projectPromptType").setProperty("currentIndex", index)
        render_ready(ui)
        click(ui, page.findChild(object, "projectPromptEdit"))
        editor = page.findChild(object, "projectPromptEditor")
        ui.wait(lambda: editor.property("opened"))
        text = page.findChild(object, "projectPromptText")
        assert not text.property("readOnly")
        text.setProperty("text", "统一描述型素描测试：清楚空间与动作，线条为主。")
        click(ui, dialog_action(ui, page, "projectPromptEditor", "projectPromptSave"))
        ui.wait(lambda: not editor.property("opened") and not page.property("promptSaving"))
        assert "后续生成使用新约定" in page.property("promptMessage")
        assert next(row for row in ops.load_prompt_settings()["prompts"] if row["id"] == "sketch_style")["effective"] == "统一描述型素描测试：清楚空间与动作，线条为主。"
        assert not ui.errors
