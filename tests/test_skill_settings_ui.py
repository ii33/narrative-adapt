"""技能面板：真实 QML 里的列表、只读内置技能、新建/编辑/停用/删除与坏文件。

技能文件一律通过 ``NARRATIVE_SKILLS_FILE`` 指到 tmp_path，绝不碰真实用户目录
``~/.narrative/skills.json``（xdist 下多个 worker 会互相覆盖用户配置）。
面板的数据全部来自 ``bridge.skills()``，所以这里断言的就是界面真正显示的东西。
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

BUILTIN_NAMES = ["分场与分镜", "人物一致性检查", "外部工具调用规范"]


def _value(item, name):
    """QML 属性读成 Python 值：JS 数组/对象读回来可能是 QJSValue，统一折一下。"""
    value = item.property(name)
    return value.toVariant() if hasattr(value, "toVariant") else value


def _visuals(item):
    """按可见树找控件：ListView 的 delegate 不在 QObject 树上，findChild 找不到它。"""
    yield item
    for child in item.childItems():
        yield from _visuals(child)


def _click(ui, item):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(ui["window"], Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point.toPoint())
    ui["settle"]()


def _evaluate(item, expression):
    """在 panel 的 QML 上下文里执行一条表达式。

    用来读「只能由 QML 算出来」的说明文字（例如内置技能那行 tooltip 的内容）；
    用真实上下文执行，走的就是界面上那条代码路径。
    """
    from PySide6.QtQml import QQmlEngine, QQmlExpression

    context = QQmlEngine.contextForObject(item)
    assert context is not None, "拿不到 QML 上下文"
    expr = QQmlExpression(context, item, expression)
    value = expr.evaluate()
    assert not expr.hasError(), expr.error().toString()
    # PySide 把 QVariant 折成 (值, 是否未定义) 交给 Python；取第一项即可。
    if isinstance(value, tuple):
        value = value[0]
    return value.toVariant() if hasattr(value, "toVariant") else value


def write_user_skill(path: Path, name: str, summary: str = "一句话摘要",
                     instructions: str = "先读清单，再逐条核对。", families=None,
                     tools=None, enabled: bool = True) -> Path:
    """按磁盘格式直接造一条用户技能：面板要能读的就是这份 JSON。"""
    payload = {"version": 1, "skills": [{
        "name": name,
        "summary": summary,
        "instructions": instructions,
        "families": list(families or []),
        "tools": list(tools or []),
        "enabled": enabled,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


@contextmanager
def skill_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("QT_QUICK_BACKEND", "software")
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    monkeypatch.setenv("NARRATIVE_APPEARANCE_FILE", str(tmp_path / "appearance.json"))
    # 技能面板与 MCP 面板同住一个设置页：MCP 的配置也指进 tmp_path，
    # 免得别的面板读到真实用户目录。
    monkeypatch.setenv("NARRATIVE_MCP_FILE", str(tmp_path / "mcp-servers.json"))
    config = tmp_path / "skills.json"
    monkeypatch.setenv("NARRATIVE_SKILLS_FILE", str(config))

    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from narrative.native_app import QML_PATH
    from narrative.native_bridge import NativeBridge

    app = QApplication.instance() or QApplication([])
    bridge = NativeBridge()
    errors: list[tuple[str, str]] = []
    notices: list[str] = []
    changed: list[int] = []
    bridge.errorOccurred.connect(lambda kind, message: errors.append((kind, message)))
    bridge.noticeRaised.connect(notices.append)
    bridge.skillsChanged.connect(lambda: changed.append(1))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_PATH)))
    assert engine.rootObjects(), "Main.qml 没有加载出来"
    window = engine.rootObjects()[0]
    page = window.findChild(object, "settingsPage")
    panel = page.findChild(object, "skillSettingsPanel")
    assert panel is not None, "设置页里没有技能面板"

    def wait(check, message="技能面板没有稳定下来"):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            app.processEvents()
            if check():
                return
            time.sleep(0.01)
        raise AssertionError(f"{message}；errors={errors}")

    def settle():
        for _ in range(6):
            app.processEvents()
            time.sleep(0.01)

    def show_panel():
        """打开设置页的「技能」分组：不切的话面板不可见，点击落不到控件上。

        下标与 SettingsPage.sections 的顺序绑定（外观 / 模型渠道 / MCP / 技能 / 关于）；
        顺序改了而这里没跟上，tests/test_settings_page_ui.py 的映射用例会先报。
        """
        window.setProperty("mode", "settings")
        page.setProperty("sectionIndex", 3)
        wait(lambda: panel.property("visible"))

    def wait_changed(count=1):
        wait(lambda: len(changed) >= count and not panel.property("busy"),
             "保存结果没有回来")

    def rows():
        return _value(panel, "skills") or []

    def row_items(name):
        """按 objectName 收集可见树里的控件，顺序即列表行顺序（内置在前，用户在后）。

        从 window 的内容根往下找而不是从 panel：编辑器是 Popup，它的内容挂在窗口的浮层上，
        不在 panel 的可见子树里；列表 delegate 又不在 QObject 树上，只能走 childItems。
        """
        return [item for item in _visuals(window.contentItem())
                if item.objectName() == name]

    def visual(name):
        found = row_items(name)
        assert found, "界面里找不到 " + name
        return found[0]

    def editor():
        return panel.findChild(object, "skillEditor")

    def open_editor():
        _click(ui, panel.findChild(object, "skillAddSkill"))
        wait(lambda: editor().property("opened"), "「新建技能」没有打开编辑器")

    def save_editor():
        _click(ui, panel.findChild(object, "skillSave"))

    ui = {
        "app": app, "window": window, "page": page, "panel": panel, "bridge": bridge,
        "errors": errors, "notices": notices, "changed": changed, "config": config,
        "wait": wait, "settle": settle, "show_panel": show_panel, "rows": rows,
        "wait_changed": wait_changed, "row_items": row_items, "visual": visual,
        "editor": editor, "open_editor": open_editor, "save_editor": save_editor,
    }
    try:
        yield ui
    finally:
        bridge.close()


def test_panel_lists_three_builtin_skills_and_they_are_read_only(tmp_path, monkeypatch):
    with skill_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        rows = ui["rows"]()
        assert [row["name"] for row in rows] == BUILTIN_NAMES
        assert all(row["builtin"] is True for row in rows)
        # 内置技能自己就能打开工具：列表上「会打开 N 个工具」这一列不是装饰。
        assert all(row["opens"]["resolved_tools"] for row in rows)
        assert _value(ui["panel"], "loadError") == ""
        assert _value(ui["panel"], "configPath").endswith("skills.json")
        # 编辑器的可选项来自注册表，和「有没有已保存的技能」无关。
        assert len(_value(ui["panel"], "families")) > 0
        assert len(_value(ui["panel"], "tools")) > 0

        # 编辑/删除的点击区对内置行一律禁用；标签本身仍接得住悬停，
        # 所以 tooltip 能解释「为什么不能改」。
        for name in ("skillRowEditHit", "skillRowDeleteHit"):
            hits = ui["row_items"](name)
            assert len(hits) == 3
            assert [item.property("enabled") for item in hits] == [False, False, False]
        assert "随程序发布" in _evaluate(ui["panel"], "rowEditTip(skills[0])")
        assert "随程序发布" in _evaluate(ui["panel"], "rowDeleteTip(skills[0])")
        # 非内置的提示语不该带这句，否则等于把「只读」说成了通用规则。
        assert "随程序发布" not in _evaluate(
            ui["panel"], "rowEditTip({name: '自定义', builtin: false})")

        # 点内置行的「编辑」什么都不发生：编辑器不打开，也不该冒出错误。
        _click(ui, ui["row_items"]("skillRowEdit")[0])
        ui["settle"]()
        assert ui["editor"]().property("opened") is False
        assert ui["errors"] == []


def test_new_skill_appears_in_list_file_and_shows_opens_preview(tmp_path, monkeypatch):
    with skill_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        ui["open_editor"]()
        panel = ui["panel"]
        panel.setProperty("draftName", "场表速查")
        panel.setProperty("draftSummary", "把场表写对的最小步骤")
        panel.setProperty("draftInstructions", "先 read_project_state，再 submit_scene_table。")

        # 业务组用点选：勾上 activity 组，预览应当立刻给出会打开的工具。
        _click(ui, ui["visual"]("skillFamily_activity"))
        assert _value(panel, "draftFamilies") == ["activity"]
        assert _value(panel, "previewTools"), "预览应当给出会打开的工具"
        assert _value(panel, "previewProblem") == ""

        ui["save_editor"]()
        ui["wait_changed"]()

        rows = ui["rows"]()
        assert len(rows) == 4
        created = rows[-1]
        assert created["name"] == "场表速查"
        assert created["builtin"] is False
        assert created["families"] == ["activity"]
        assert created["opens"]["resolved_tools"], "列表里也要能看到这条技能会打开什么"

        payload = json.loads(ui["config"].read_text(encoding="utf-8"))
        assert [row["name"] for row in payload["skills"]] == ["场表速查"]
        assert payload["skills"][0]["instructions"].startswith("先 read_project_state")
        assert payload["skills"][0]["enabled"] is True

        assert ui["errors"] == []
        assert any("已保存" in notice for notice in ui["notices"])


def test_editing_summary_is_written_back(tmp_path, monkeypatch):
    config = write_user_skill(tmp_path / "skills.json", "场表速查", summary="旧摘要")
    with skill_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        assert ui["rows"]()[-1]["name"] == "场表速查"

        # 内置三条在前、用户技能在最后：点最后一行那个「编辑」。
        edits = ui["row_items"]("skillRowEditHit")
        assert len(edits) == 4
        _click(ui, edits[-1])
        ui["wait"](lambda: ui["editor"]().property("opened"), "行内「编辑」没有打开编辑器")
        # 名称是身份：编辑已有技能时只读，改不了（内核也没有改名接口）。
        assert ui["panel"].findChild(object, "skillName").property("readOnly") is True
        assert ui["panel"].findChild(object, "skillName").property("text") == "场表速查"
        # 正文与摘要要真的灌进输入框：输入框的 text 不绑定草稿，漏灌一次就会显示上一条的内容。
        assert ui["panel"].findChild(object, "skillSummary").property("text") == "旧摘要"
        assert (ui["panel"].findChild(object, "skillInstructions").property("text")
                == "先读清单，再逐条核对。")

        ui["panel"].setProperty("draftSummary", "新摘要")
        ui["save_editor"]()
        ui["wait_changed"]()

        assert ui["rows"]()[-1]["summary"] == "新摘要"
        payload = json.loads(config.read_text(encoding="utf-8"))
        assert payload["skills"][0]["summary"] == "新摘要"


def test_disable_switch_takes_effect(tmp_path, monkeypatch):
    config = write_user_skill(tmp_path / "skills.json", "场表速查")
    with skill_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        switches = ui["row_items"]("skillRowEnabled")
        assert len(switches) == 4
        # 内置三行的开关是禁用的：内核拒绝改内置技能，界面先别给机会。
        assert [item.property("enabled") for item in switches[:3]] == [False, False, False]
        assert switches[-1].property("checked") is True

        _click(ui, switches[-1])
        ui["wait_changed"]()
        assert ui["rows"]()[-1]["enabled"] is False
        assert json.loads(config.read_text(encoding="utf-8"))["skills"][0]["enabled"] is False

        # 再点回来：启用状态可以来回切，不是单向的。
        _click(ui, ui["row_items"]("skillRowEnabled")[-1])
        ui["wait_changed"](2)
        assert ui["rows"]()[-1]["enabled"] is True
        assert json.loads(config.read_text(encoding="utf-8"))["skills"][0]["enabled"] is True
        assert ui["errors"] == []


def test_delete_user_skill_succeeds_and_builtin_is_refused(tmp_path, monkeypatch):
    config = write_user_skill(tmp_path / "skills.json", "场表速查")
    with skill_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        deletes = ui["row_items"]("skillRowDeleteHit")
        assert len(deletes) == 4
        assert [item.property("enabled") for item in deletes[:3]] == [False, False, False]

        _click(ui, deletes[-1])
        ui["wait_changed"]()
        assert [row["name"] for row in ui["rows"]()] == BUILTIN_NAMES
        assert json.loads(config.read_text(encoding="utf-8"))["skills"] == []
        assert any("已删除" in notice for notice in ui["notices"])

        # 绕过界面直接删内置技能（例如有人从控制台调）：内核拒绝，
        # 错误消息要能直接摆到界面上——说清是内置、说了删不掉、还提了替代做法。
        ui["bridge"].deleteSkill("分场与分镜")
        ui["wait"](lambda: ui["errors"], "删除内置技能没有被拒绝")
        kind, message = ui["errors"][-1]
        assert kind == "skill"
        assert "内置" in message and "不可删除" in message
        # 被拒之后列表原样：内置三条都还在。
        assert [row["name"] for row in ui["rows"]()] == BUILTIN_NAMES


def test_rejected_draft_shows_error_and_keeps_the_editor_open(tmp_path, monkeypatch):
    """界面不重复实现校验：超长摘要由内核拒绝，错误就地显示，草稿不丢。"""
    with skill_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        ui["open_editor"]()
        ui["panel"].setProperty("draftName", "摘要太长")
        ui["panel"].setProperty("draftSummary", "长" * 201)  # 上限 200，界面不截断
        ui["panel"].setProperty("draftInstructions", "先读清单，再核对。")

        ui["save_editor"]()
        ui["wait"](lambda: ui["errors"], "超长摘要没有被拒绝")
        kind, message = ui["errors"][-1]
        assert kind == "skill"
        assert "200" in message

        # 失败后编辑器重新打开，草稿原样还在：别让用户重敲一遍。
        ui["wait"](lambda: ui["editor"]().property("opened"), "保存失败后编辑器没有重新打开")
        assert _value(ui["panel"], "draftName") == "摘要太长"
        assert _value(ui["panel"], "message") == message
        assert _value(ui["panel"], "messageIsError") is True
        # 被拒的草稿不能留半条记录：磁盘上什么都没写。
        assert not ui["config"].exists()


def test_broken_config_shows_readable_error_instead_of_crashing(tmp_path, monkeypatch):
    config = tmp_path / "skills.json"
    broken = '{"skills": [ 这不是 JSON'
    config.write_text(broken, encoding="utf-8")
    with skill_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        message = _value(ui["panel"], "loadError")
        assert "skills.json" in message
        assert "JSON" in message
        # 读不懂时列表为空，但错误必须已经说出来（而不是显示「没有技能」）。
        assert ui["rows"]() == []
        assert ui["panel"].findChild(object, "skillLoadError").property("visible") is True
        # 坏文件不动：面板只显示错误，不能顺手把它覆盖掉。
        assert config.read_text(encoding="utf-8") == broken
