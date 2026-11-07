"""Shared read-only image-layout advice without altering generation or project state."""
import json
from types import SimpleNamespace

import pytest
from agent_fixtures import project as project

from narrative import tool_api
from narrative.tool_loading import CORE_TOOL_NAMES, list_available_tools, load_tools, tool_guidance


def _files(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_help_requires_no_scene_and_does_not_touch_project_files(project, tmp_path):
    before = _files(tmp_path)
    tips = project.read_storyboard_usage_tips()
    assert tips["id"] == "spatial-layout-generation"
    assert tips["version"] == "1"
    assert tips["sections"] and tips["current_workflow"] and tips["scope"]
    assert _files(tmp_path) == before


def test_help_values_are_independent_and_not_report_files(project, monkeypatch):
    monkeypatch.setattr(project._store, "connect", lambda *a, **k: pytest.fail("help accessed database"))
    first = project.read_storyboard_usage_tips()
    first["sections"][0]["paragraphs"].clear()
    first["sections"].clear()
    second = project.read_storyboard_usage_tips()
    assert second["sections"][0]["paragraphs"]
    assert len({s["id"] for s in second["sections"]}) == len(second["sections"])
    assert all(s["title"] and all(isinstance(p, str) and p for p in s["paragraphs"]) for s in second["sections"])
    assert "artifacts/" not in json.dumps(second)


def test_canonical_help_tool_is_readonly_and_demand_loaded(project):
    name = "read_storyboard_usage_tips"
    tool = tool_api.TOOLS[name]
    assert tool.writes is False and name not in CORE_TOOL_NAMES
    assert tool.schema() == {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    assert tool.handler(project, {}) == project.read_storyboard_usage_tips()
    manifest = list_available_tools(names=[name])["tools"]
    assert manifest[0]["family"] == "storyboard"
    loaded = load_tools(names=[name], ops=project)
    assert loaded["resolved_tools"] == [name]
    assert "workflow_guidance" not in loaded
    assert loaded["tools"][0]["inputSchema"] == tool.schema()


def test_image_tools_receive_only_short_help_discovery_hint(project):
    names = ["generate_storyboard_sketch", "generate_storyboard_panel", "generate_storyboard_panels", "render_storyboard_style"]
    for name in names:
        guide = tool_guidance(project, [name])
        assert set(guide) == {"usage_tips"}
        assert guide["usage_tips"]["operation"] == "read_storyboard_usage_tips"
        assert len(guide["usage_tips"]["message"]) < 180
    assert tool_guidance(project, ["read_storyboard_usage_tips"]) == {}
    assert tool_guidance(project, ["read_storyboard"]) == {}


def test_native_help_error_remains_scoped_to_help_request():
    from narrative.native_bridge import NativeBridge

    def fail():
        raise RuntimeError("help loading failed")

    replies = []
    from unittest.mock import patch

    from narrative import project_catalog

    fake = SimpleNamespace(
        _project=lambda: "isolated.nproj",
        _submit=lambda event, callback: replies.append((event, callback())),
    )
    with patch.object(project_catalog, "open_graph_project", return_value=SimpleNamespace(read_storyboard_usage_tips=fail)):
        NativeBridge.storyboardAction(fake, "read_storyboard_usage_tips", {}, 407)
    assert replies == [("storyboard", {"token": 407, "operation": "read_storyboard_usage_tips", "error": "help loading failed", "error_type": "RuntimeError"})]


@pytest.mark.parametrize("stage", ["project", "open", "submit"])
def test_native_help_opening_errors_keep_operation_token(stage, monkeypatch):
    from narrative import project_catalog
    from narrative.native_bridge import NativeBridge

    replies = []

    def fail(*args, **kwargs):
        raise RuntimeError("isolated help error")

    fake = SimpleNamespace(
        _project=fail if stage == "project" else lambda: "isolated.nproj",
        _submit=fail if stage == "submit" else lambda event, callback: replies.append((event, callback())),
        resultReady=SimpleNamespace(emit=lambda event, payload: replies.append((event, payload))),
    )
    monkeypatch.setattr(project_catalog, "open_graph_project", fail)
    NativeBridge.storyboardAction(fake, "read_storyboard_usage_tips", {}, 408)
    assert replies == [("storyboard", {"token": 408, "operation": "read_storyboard_usage_tips", "error": "isolated help error", "error_type": "RuntimeError"})]


def test_native_help_without_open_project_returns_scoped_value_error():
    from narrative.native_bridge import NativeBridge

    replies = []
    fake = SimpleNamespace(
        _project=lambda: NativeBridge._project(SimpleNamespace(_project_path=None)),
        resultReady=SimpleNamespace(emit=lambda event, payload: replies.append((event, payload))),
    )
    NativeBridge.storyboardAction(fake, "read_storyboard_usage_tips", {}, 409)
    assert replies == [("storyboard", {"token": 409, "operation": "read_storyboard_usage_tips", "error": "请先打开图项目", "error_type": "ValueError"})]
