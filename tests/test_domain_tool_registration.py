"""Contracts for independently maintained tool catalogs."""
import pytest

from narrative import adaptation_tools, asset_tools, tool_api
from narrative.graph_operations import GraphOperations


EXPECTED_ADAPTATION_TOOLS = {
    "adaptation_snapshot", "check_adaptation", "load_project_style", "save_project_style",
    "load_prompt_settings", "save_prompt_override", "import_adaptation_candidates",
    "import_adaptation", "adaptation_browse_view", "adaptation_node_properties",
    "adaptation_node_editor", "adaptation_node_detail", "adaptation_node_assets",
    "sync_adaptation", "edit_adaptation_node", "copy_adaptation_node", "move_adaptation_node",
    "delete_adaptation_node", "suppress_adaptation_node", "set_adaptation_property",
    "delete_adaptation_property_changes", "list_assets", "get_asset", "save_asset",
    "delete_asset", "asset_moments",
}


def test_catalog_is_complete_and_operations_exist():
    tools = []
    for catalog in (adaptation_tools, asset_tools):
        catalog.register(tool_api.Tool, tools.append, tool_api._validate_adaptation_argument, tool_api.ToolError)
    names = [tool.name for tool in tools]
    assert set(names) == EXPECTED_ADAPTATION_TOOLS
    assert len(names) == len(set(names))
    assert all(callable(getattr(GraphOperations, name, None)) for name in names)
    assert all(name in tool_api.TOOLS for name in names)
    assert all(tool_api.TOOLS[tool.name].schema() == tool.schema() for tool in tools)


def test_duplicate_registration_is_rejected_without_overwriting():
    original = tool_api.TOOLS["adaptation_snapshot"]
    with pytest.raises(ValueError, match="Duplicate tool registration"):
        tool_api._register(original)
    assert tool_api.TOOLS[original.name] is original


def test_missing_operation_does_not_silently_succeed():
    with pytest.raises(AttributeError, match="adaptation_snapshot"):
        tool_api.TOOLS["adaptation_snapshot"].handler(object(), {})
