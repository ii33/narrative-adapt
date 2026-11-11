"""Real QML multi-shot asset selection, analysis scope and asset navigation."""
from test_chat_scroll import transcript_shell
from test_screenplay_ui import render_ready, visuals
from test_static_check_ui import click, plain


def seed(ops):
    source = ops.import_source("视觉元素", "甲在门口停下。")
    saved = ops.submit_extraction(source, {"nodes": [
        {"type": "person", "name": "甲", "quotes": ["甲在门口停下。"]},
        {"type": "location", "name": "门口", "quotes": ["门口"]},
        {"type": "scene", "name": "门口停步"},
        {"type": "event", "name": "停步", "scene": "@3", "location": "@2", "participants": ["@1"], "quotes": ["甲在门口停下。"]},
    ], "properties": []})
    assert saved["status"] == "accepted", saved
    event = next(n["id"] for n in ops.graph_snapshot()["nodes"] if n["node_type"] == "event")
    person = next(n["id"] for n in ops.graph_snapshot()["nodes"] if n["node_type"] == "person")
    adaptation = ops.import_adaptation([event], 0, "视觉元素隔离测试")
    screenplay = ops.create_screenplay("视觉元素测试", reason="测试")["screenplay"]
    timeline = ops.import_screenplay_timeline(screenplay["id"], [event], 0, adaptation["revision"], reason="测试")
    span = ops.create_adaptation_span(screenplay["id"], "停步", selection_ids=[event], expected_timeline_revision=timeline["revision"], reason="测试")["span"]
    segment = ops.create_script_segment(screenplay["id"], span["id"], "停步", screenplay["structure_revision"], reason="测试")
    context = ops.read_scene_table_context(segment["id"])
    table = ops.submit_scene_table(segment["id"], [{"plan": {"title": "门口", "summary": "甲停步。"}}], context["expected_outline_revision"], context["span_fingerprint"], context["context_fingerprint"], reason="测试")
    unit = table["units"][0]
    asset = ops.save_asset({"category": "appearance", "domain": "extraction", "node_id": person, "title": "形象2"})
    board = ops.apply_storyboard_changes(unit["id"], 0, [
        {"op": "create_shot", "title": "停步", "duration_ms": 1000, "visual_elements": [{"kind": "person", "name": "甲", "usage": "停步", "asset_id": asset["id"]}]},
        {"op": "create_shot", "title": "门口", "duration_ms": 1000, "visual_elements": [{"kind": "prop", "name": "钥匙", "usage": "手持", "asset_id": None}]},
    ])
    return screenplay["id"], unit["id"], board, asset


def open_desk(ui, screenplay):
    from PySide6.QtCore import Q_ARG, QMetaObject
    ui.window.setProperty("assistantOpen", False)
    ui.window.setProperty("currentPage", "screenplay")
    host = ui.window.findChild(object, "screenplayPage")
    QMetaObject.invokeMethod(host, "selectScreenplay", Q_ARG("QVariant", screenplay))
    ui.wait(lambda: plain(host.property("screenplay")).get("id") == screenplay)
    ui.window.setProperty("currentPage", "storyboard")
    desk = ui.window.findChild(object, "storyboardPage")
    ui.wait(lambda: desk.property("ready") and len(plain(desk.property("shots"))) == 2)
    render_ready(ui)
    return desk


def test_multiselect_all_and_fixed_analysis_target(tmp_path, monkeypatch):
    from PySide6.QtCore import Q_ARG, QMetaObject

    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        captured = []
        monkeypatch.setattr(ui.bridge, "_assistant_turn", lambda *a, **kw: captured.append(kw["writing_task"]))
        desk = open_desk(ui, screenplay)
        QMetaObject.invokeMethod(desk, "toggleAssetShot", Q_ARG("QVariant", board["shots"][1]["id"]), Q_ARG("QVariant", True))
        assert plain(desk.property("assetShotIds")) == [board["shots"][1]["id"]]
        click(ui, desk.findChild(object, "storyboardSelectAllAssets"))
        assert len(plain(desk.property("assetShotIds"))) == 2
        click(ui, desk.findChild(object, "storyboardAnalyzeAssets"))
        ui.wait(lambda: bool(captured))
        assert captured[0]["target"]["kind"] == "storyboard_assets"
        assert captured[0]["target"]["shot_ids"] == [s["id"] for s in board["shots"]]
        assert desk.property("assetAnalysisPending")
        ui.bridge.resultReady.emit("conversation_progress", {"event": "task_complete", "project_path": str(ui.bridge._project_path), "writing_target": captured[0]["target"]})
        ui.wait(lambda: not desk.property("assetAnalysisPending"))
        assert not ui.errors


def test_element_table_missing_hint_and_asset_domain_navigation(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, _unit, _board, asset = seed(ops)
        desk = open_desk(ui, screenplay)
        label = next(x for x in visuals(desk) if x.objectName() == "storyboardAssetElementLabel")
        assert "甲" in label.property("text") and "形象2" in label.property("text")
        assert "图" in label.property("text")  # Existing card has no image yet.
        labels = [x.property("text") for x in visuals(desk) if x.objectName() == "storyboardAssetElementLabel"]
        assert any("钥匙" in text and ("缺" in text or "未绑定" in text) for text in labels), labels
        click(ui, next(x for x in visuals(desk) if x.objectName() == "storyboardOpenElementAsset"))
        page = ui.window.findChild(object, "assetsPage")
        ui.wait(lambda: page.property("editing") and plain(page.property("card")).get("id") == asset["id"])
        assert page.property("domain") == "extraction"
        page.findChild(object, "assetTitle").setProperty("text", "形象2修改")
        click(ui, page.findChild(object, "assetSave"))
        ui.wait(lambda: ops.get_asset(asset["id"])["title"] == "形象2修改")
        assert ops.get_asset(asset["id"])["domain"] == "extraction"
        assert not ui.errors


def test_independent_asset_can_be_created_without_fact_node(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        ui.window.setProperty("assistantOpen", False)
        ui.window.setProperty("currentPage", "assets")
        page = ui.window.findChild(object, "assetsPage")
        ui.wait(lambda: plain(page.property("payload")).get("category") == "appearance")
        click(ui, page.findChild(object, "assetAdd"))
        page.findChild(object, "assetTitle").setProperty("text", "陌生人形象")
        click(ui, page.findChild(object, "assetSave"))
        ui.wait(lambda: len(ops.list_assets("appearance")["assets"]) == 1)
        card = ops.list_assets("appearance")["assets"][0]
        assert card["title"] == "陌生人形象" and card["node_id"] == ""
        assert not ops.graph_snapshot()["nodes"]


def test_start_failure_recovers_but_stale_failure_does_not_unlock_new_task(tmp_path, monkeypatch):
    from PySide6.QtCore import Q_ARG, QMetaObject

    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, _unit, board, _asset = seed(ops)
        captured = []
        monkeypatch.setattr(ui.bridge, "_assistant_turn", lambda *a, **kw: captured.append(kw["writing_task"]))
        desk = open_desk(ui, screenplay)
        QMetaObject.invokeMethod(desk, "toggleAssetShot", Q_ARG("QVariant", board["shots"][0]["id"]), Q_ARG("QVariant", True))
        click(ui, desk.findChild(object, "storyboardAnalyzeAssets"))
        ui.wait(lambda: len(captured) == 1)
        failure = {"target": captured[0]["target"], "project_path": str(ui.bridge._project_path), "task_id": "", "error": "启动时已有任务"}
        ui.bridge.resultReady.emit("screenplay_writing_failed", failure)
        ui.wait(lambda: not desk.property("assetAnalysisPending"))
        assert "已有任务" in desk.property("message")
        click(ui, desk.findChild(object, "storyboardAnalyzeAssets"))
        ui.wait(lambda: len(captured) == 2)
        assert captured[1]["target"]["client_request_token"] != captured[0]["target"]["client_request_token"]
        ui.bridge.resultReady.emit("screenplay_writing_failed", failure)
        ui.settle()
        assert desk.property("assetAnalysisPending")
        ui.bridge.resultReady.emit("screenplay_writing_failed", failure | {"target": captured[1]["target"], "project_path": "C:/other/project.nproj"})
        ui.settle()
        assert desk.property("assetAnalysisPending")
        ui.bridge.resultReady.emit("screenplay_writing_failed", failure | {"target": captured[1]["target"]})
        ui.wait(lambda: not desk.property("assetAnalysisPending"))
        assert not ui.errors


def open_create_element(ui, desk, shot_id):
    from PySide6.QtCore import Q_ARG, QMetaObject
    script = desk.findChild(object, "storyboardScriptView")
    QMetaObject.invokeMethod(script, "createElement", Q_ARG("QVariant", shot_id), Q_ARG("QVariant", 0))
    dialog = script.findChild(object, "storyboardCreateElementAssetDialog")
    ui.wait(lambda: dialog.property("opened") and dialog.property("revision") >= 0)
    render_ready(ui)
    return script, dialog


def test_element_create_is_manual_and_binds_only_after_save(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        desk = open_desk(ui, screenplay)
        assert not ops.list_assets("item")["assets"]
        script, dialog = open_create_element(ui, desk, board["shots"][1]["id"])
        assert script.findChild(object, "storyboardCreateElementAssetTitle").property("text") == "钥匙"
        assert not ops.list_assets("item")["assets"]  # Opening the form creates nothing.
        script.findChild(object, "storyboardCreateElementAssetTitle").setProperty("text", "铜钥匙测试")
        click(ui, script.findChild(object, "storyboardSaveElementAsset"))
        ui.wait(lambda: not script.property("elementActionPending"))
        ui.settle()
        assert dialog.property("errorText") == ""
        asset_id = ops.read_storyboard(unit)["shots"][1]["visual_elements"][0]["asset_id"]
        assert asset_id
        assert ops.get_asset(asset_id)["title"] == "铜钥匙测试"
        assert ops.get_asset(asset_id)["node_id"] == ""
        ui.wait(lambda: not dialog.property("opened"))
        assert not ui.errors


def test_existing_asset_is_selected_from_element_list(tmp_path, monkeypatch):
    from PySide6.QtCore import Q_ARG, QMetaObject

    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        asset = ops.save_asset({"category": "item", "title": "已有钥匙", "node_id": "", "domain": "adaptation"})
        desk = open_desk(ui, screenplay)
        script = desk.findChild(object, "storyboardScriptView")
        QMetaObject.invokeMethod(script, "chooseElement", Q_ARG("QVariant", board["shots"][1]["id"]), Q_ARG("QVariant", 0), Q_ARG("QVariant", "prop"))
        dialog = script.findChild(object, "storyboardVisualAssetPicker")
        ui.wait(lambda: dialog.property("opened") and len(plain(dialog.property("assets"))) == 1)
        render_ready(ui)
        option = next(x for x in visuals(dialog.property("contentItem")) if x.objectName() == "storyboardElementAssetOption")
        click(ui, option)
        ui.wait(lambda: not script.property("elementActionPending"))
        ui.settle()
        assert ops.read_storyboard(unit)["shots"][1]["visual_elements"][0]["asset_id"] == asset["id"]
        assert len(ops.list_assets("item")["assets"]) == 1
        assert not ui.errors


def test_asset_created_but_stale_binding_is_reported_without_retry(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        desk = open_desk(ui, screenplay)
        script, dialog = open_create_element(ui, desk, board["shots"][1]["id"])
        ops.apply_storyboard_changes(unit, board["revision"], [{"op": "update_shot", "shot_id": board["shots"][1]["id"], "description": "其他窗口已修改"}])
        click(ui, script.findChild(object, "storyboardSaveElementAsset"))
        ui.wait(lambda: bool(ops.list_assets("item")["assets"]) and not script.property("elementActionPending"))
        ui.wait(lambda: "已建立" in dialog.property("errorText"))
        assert "关联未完成" in dialog.property("errorText")
        assert ops.read_storyboard(unit)["shots"][1]["visual_elements"][0]["asset_id"] is None
        assert len(ops.list_assets("item")["assets"]) == 1
        assert not script.findChild(object, "storyboardSaveElementAsset").property("enabled")


def test_element_asset_staged_form_repairs_same_submission_then_binds(tmp_path, monkeypatch):
    from PySide6.QtCore import QMetaObject

    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        desk = open_desk(ui, screenplay)
        script, dialog = open_create_element(ui, desk, board["shots"][1]["id"])
        state = dialog.findChild(object, "storyboardElementAssetSubmission")
        assert state.property("active")
        # Deliver an invalid title at the save boundary, exercising the real
        # Operations staging route even though ordinary blank saves are disabled.
        script.findChild(object, "storyboardCreateElementAssetTitle").setProperty("text", " ")
        QMetaObject.invokeMethod(script.findChild(object, "storyboardSaveElementAsset"), "clicked")
        ui.wait(lambda: bool(plain(state.property("submission")).get("submission_id")))
        assert dialog.property("opened") and not script.property("elementActionPending")
        submission = plain(state.property("submission"))
        assert not ops.list_assets("item")["assets"]
        script.findChild(object, "storyboardCreateElementAssetTitle").setProperty("text", "修好的钥匙")
        click(ui, script.findChild(object, "storyboardSaveElementAsset"))
        ui.wait(lambda: not script.property("elementActionPending"))
        ui.settle()
        assert ops.read_storyboard(unit)["shots"][1]["visual_elements"][0].get("asset_id")
        repaired = ops.read_content_submission(submission["submission_id"])
        assert repaired["submission_status"] == "committed" and repaired["submission_revision"] == 2
        assert not ops.list_content_submissions(operation="save_asset")["submissions"]
        assert ops.list_assets("item")["assets"][0]["title"] == "修好的钥匙"
        assert not ui.errors


def test_one_click_creates_selected_missing_cards_without_model_or_name_merge(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, asset = seed(ops)
        ops.apply_storyboard_changes(unit, board["revision"], [{"op": "update_shot", "shot_id": board["shots"][1]["id"], "visual_elements": [
            {"kind": "prop", "name": "钥匙", "usage": "手持", "asset_id": None},
            {"kind": "prop", "name": "钥匙", "usage": "地面旧钥匙", "asset_id": None},
        ]}])
        monkeypatch.setattr(ui.bridge, "_assistant_turn", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not invoke model")))
        desk = open_desk(ui, screenplay)
        click(ui, desk.findChild(object, "storyboardSelectAllAssets"))
        assert desk.property("selectedMissingAssetCount") == 2
        assert not ops.list_assets("item")["assets"]
        click(ui, desk.findChild(object, "storyboardCreateMissingAssets"))
        ui.wait(lambda: not desk.property("missingAssetsPending"))
        ui.settle()
        assert len(ops.list_assets("item")["assets"]) == 2
        cards = ops.list_assets("item")["assets"]
        assert cards[0]["id"] != cards[1]["id"] and all(c["title"].startswith("钥匙") for c in cards)
        assert all(not c["images"] and not c["node_id"] for c in cards)
        assert all(c["detail"] == "" for c in cards)
        assert {e["usage"] for e in ops.read_storyboard(unit)["shots"][1]["visual_elements"]} == {"手持", "地面旧钥匙"}
        ui.wait(lambda: desk.property("selectedMissingAssetCount") == 0)
        assert ops.read_storyboard(unit)["shots"][0]["visual_elements"][0]["asset_id"] == asset["id"]
        assert "2" in desk.property("message")
        assert not ui.errors


def test_one_click_row_creates_only_that_element(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        ops.apply_storyboard_changes(unit, board["revision"], [{"op": "update_shot", "shot_id": board["shots"][1]["id"], "visual_elements": [
            {"kind": "prop", "name": "钥匙", "usage": "手持", "asset_id": None},
            {"kind": "set", "name": "门口", "usage": "背景", "asset_id": None},
        ]}])
        desk = open_desk(ui, screenplay)
        button = next(x for x in visuals(desk) if x.objectName() == "storyboardCreateElementAsset" and x.property("elementIndex") == 1)
        # Bring the second row into the viewport before the actual click.
        rows = desk.findChild(object, "storyboardScriptRows")
        rows.positionViewAtIndex(1, 0)
        render_ready(ui)
        click(ui, button)
        ui.wait(lambda: not desk.property("missingAssetsPending"))
        ui.settle()
        assert len(ops.list_assets("location")["assets"]) == 1
        elements = ops.read_storyboard(unit)["shots"][1]["visual_elements"]
        assert elements[0]["asset_id"] is None and elements[1]["asset_id"]
        assert not ops.list_assets("item")["assets"]
        assert not ui.errors


def test_one_click_stale_board_rolls_back_and_releases_progress(tmp_path, monkeypatch):
    from narrative.operations import Operations
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        screenplay, unit, board, _asset = seed(ops)
        desk = open_desk(ui, screenplay)
        click(ui, desk.findChild(object, "storyboardSelectAllAssets"))
        ops.apply_storyboard_changes(unit, board["revision"], [{"op": "update_shot", "shot_id": board["shots"][1]["id"], "description": "并发变化"}])
        click(ui, desk.findChild(object, "storyboardCreateMissingAssets"))
        ui.wait(lambda: not desk.property("missingAssetsPending") and bool(desk.property("message")))
        assert not ops.list_assets("item")["assets"]
        assert ops.read_storyboard(unit)["shots"][1]["visual_elements"][0]["asset_id"] is None
