"""Suite selection: the default run stays fast; heavy UI/SDK suites are opt-in.

New tests: assert data correctness first. Pure functions (state derivation,
validation, draft normalization) need no project. Build an environment only when
the assertion depends on persistence, atomic writes, or revisions, and then use
the session ``project_template`` copy (see ``agent_fixtures.fresh_project``)
instead of creating a project from scratch.
"""
from __future__ import annotations

import pytest

UI_SUFFIX = "_ui.py"
NATIVE_HINTS = (
    "native",
    "sdk",
    "harness",
    "shared_conversation",
    "unbounded_conversation",
    "conversation_steering",
    "conversation_tool_loading",
    "messages_usage_compaction",
    "model_tool_schema",
    "runtime_security",
    "chat_store_concurrency",
)
# 非数据正确性的重集成用例：保留可运行，但不进默认套件。
INTEGRATION_MODULES = {
    "test_assistant_commit_summary",
    "test_storyboard_video_acceptance",
    "test_readable_tools_acceptance",
    "test_desktop_first_launch",
    "test_resident_interrupt",
    "test_host_command_output_encoding",
    "test_tool_fingerprint_refs",
    "test_usage_live_session",
    "test_usage_statistics",
    "test_screenplay_assistant_cancel",
    "test_scene_table_task_scope",
    "test_scene_table_read_scope",
    "test_tool_loading",
    "test_tool_presentation",
    "test_gateway_tool_arguments",
    "test_project_prompt_settings",
    "test_staging_acceptance_script",
    "test_assistant_access_runtime",
    "test_browse_detail_sync",
    "test_storyboard_review",
    "test_adaptation_mcp",
    "test_conversation_stop_regression",
    "test_assistant_host",
    "test_screenplay_transports",
    "test_storyboard_media_integrity",
    "test_host_text_operations",
    "test_session_host_cleanup",
    "test_content_staging_transports",
    "test_storyboard_audio",
    "test_chat_attachments",
    "test_asset_domain_bridge",
    "test_task_write_summary",
    "test_task_tool_host",
    "test_host_transports",
    "test_project_copy_transports",
    "test_chat_attachment_transports",
    "test_manual_element_asset_bridge",
    "test_storyboard_usage_tips",
    "test_host_catalog_audit",
    "test_task_prompt_scope",
    "test_reading_position_picker",
    "test_browse_unrecorded",
    "test_source_conversation_buttons",
}


def pytest_configure(config):
    config.addinivalue_line("markers", "ui: Qt Quick UI tests (slow, heavy)")
    config.addinivalue_line("markers", "native: real SDK/HTTP/host runtime tests (slow, environment sensitive)")
    config.addinivalue_line("markers", "integration: non-data integration suites kept opt-in")


def _default_run(config) -> bool:
    expression = str(getattr(config.option, "markexpr", "") or "").replace("'", "").replace('"', "")
    return "not ui" in expression and "not native" in expression and "not integration" in expression


def pytest_ignore_collect(collection_path, config):
    """默认套件不为 ui/native/integration 模块付收集与导入成本。"""
    if not _default_run(config):
        return None
    name = collection_path.name
    stem = collection_path.stem
    if name.endswith(UI_SUFFIX) or stem in INTEGRATION_MODULES or any(hint in name for hint in NATIVE_HINTS):
        return True
    try:
        source = collection_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return True if "PySide6" in source else None


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        try:
            source = (config.rootpath / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            source = ""
        if path.endswith(UI_SUFFIX) or "PySide6" in source:
            item.add_marker(pytest.mark.ui)
        elif path.split("::", 1)[0].removesuffix(".py").rsplit("/", 1)[-1] in INTEGRATION_MODULES:
            item.add_marker(pytest.mark.integration)
        elif any(hint in path for hint in NATIVE_HINTS):
            item.add_marker(pytest.mark.native)


@pytest.fixture(scope="session")
def project_template(tmp_path_factory):
    """One empty project built per session; per-test copies keep isolation cheap."""
    from narrative.operations import Operations

    root = tmp_path_factory.mktemp("project-template")
    Operations.create_graph_project(root / "unified.nproj")
    return root
