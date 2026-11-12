"""Suite selection: the default run stays fast; heavy UI/SDK suites are opt-in.

New tests: assert data correctness first. Pure functions (state derivation,
validation, draft normalization) need no project. Build an environment only when
the assertion depends on persistence, atomic writes, or revisions, and then use
the session ``project_template`` copy (see ``agent_fixtures.fresh_project``)
instead of creating a project from scratch.
"""
from __future__ import annotations

import os

import pytest

# UI 测试一律离屏：QApplication 的平台在**第一次创建时**定死，谁先建谁决定整进程
# 用哪个平台。以前各 ui 文件在夹具里各自设 env，结果一个没设的文件（直接在真 Windows
# 平台上建了 QApplication）会把后面所有文件的窗口卡在屏幕可用宽度里（实测 1360/1500
# 宽的用例变成 1282）。在收集期就把默认值钉住，谁先建都一样。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

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


def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true", default=False,
                     help="连同 slow 用例一起跑（全量入口；默认只跑门禁）")


def pytest_configure(config):
    config.addinivalue_line("markers", "ui: Qt Quick UI tests (slow, heavy)")
    config.addinivalue_line("markers", "native: real SDK/HTTP/host runtime tests (slow, environment sensitive)")
    config.addinivalue_line("markers", "integration: non-data integration suites kept opt-in")
    config.addinivalue_line("markers", "slow: 单例 >8s 或 setup>50% 或涉及真实图像/子进程；默认排除，--slow 全量")
    if config.getoption("slow"):
        # 门禁的 addopts 里带着 "and not slow"；--slow 把它去掉，回到「默认集全量」。
        expression = str(getattr(config.option, "markexpr", "") or "")
        config.option.markexpr = expression.replace(" and not slow", "").strip() or "not ui and not native and not integration"


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


# 重夹具链的「模板」放在这里而不是各自的测试模块：pytest 按提出请求的模块解析
# 夹具参数，模板只有放在 conftest 才能被 `from test_x import fixture` 借用它的模块
# 解析到。模板按模块作用域建一次，用例侧一律 copytree 到自己的 tmp_path 再打开。
@pytest.fixture(scope="module")
def writing_template(tmp_path_factory):
    """剧本链条模板（原文→提取→改编→剧本→时间线→选区→段落），构造在 test_screenplay_writing。"""
    from test_screenplay_writing import build_writing_template

    return build_writing_template(tmp_path_factory)


@pytest.fixture(scope="module")
def sources_template(tmp_path_factory):
    """来源标注链条模板（原文→提取→改编→剧本→时间线→选区→段落），构造在 test_screenplay_source_references。"""
    from test_screenplay_source_references import build_sources_template

    return build_sources_template(tmp_path_factory)
