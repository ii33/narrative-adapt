"""进模型的一切都必须可读：没有 hash、没有内部路径，身份是可读编号。

两条原则在这里守：
1. 工具参数/返回值/错误消息里不出现 12 位十六进制 id、`C:\\` 绝对路径、`.runtime`。
2. 「当前会话／当前任务」由程序默认给出，模型不必传内部编号。
"""
from __future__ import annotations

import http.client
import json
import re
from urllib.parse import urlsplit

import pytest
from agent_fixtures import fresh_project

from narrative import assistant, tool_api
from narrative.chat_store import ChatStore
from narrative.model_text import scrub
from narrative.project_package import layout
from narrative.task_ids import next_task_id, readable_task_id
from narrative.task_tool_host import TaskToolHost
from narrative.tool_execution_context import bind_tool_identity

# 12 位以上十六进制（uuid 截断、sha 前缀）：字母数字都有，任何进模型的地方都不许出现。
HEX_TOKEN = re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{8,}(?![0-9A-Za-z])")
DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")


def assert_model_safe(text: str) -> None:
    assert ".runtime" not in text, text
    assert DRIVE_PATH.search(text) is None, text
    assert HEX_TOKEN.search(text) is None, text


@pytest.fixture
def project(tmp_path, monkeypatch, project_template):
    return fresh_project(tmp_path, monkeypatch, template=project_template)


def test_chat_ids_are_project_ordinals_and_legacy_files_are_not_rewritten(project):
    entry = layout(project.project_info().path).entry
    store = ChatStore(entry)
    first = store.create("第一次对话")
    second = store.create("第二次对话")
    assert (first["id"], second["id"]) == ("chat-1", "chat-2")
    assert first["session"] == "chat-1"
    # 旧工程：uuid 文件名按创建顺序补成可读编号，历史文件本身不改写。
    legacy = store.root / "def0b07f8d3d.json"
    legacy.write_text(json.dumps({"id": "def0b07f8d3d", "session": "chat-def0b07f8d3d",
                                  "title": "旧会话", "messages": [],
                                  "created_at": "2026-01-01T00:00:00.000+00:00"}, ensure_ascii=False), encoding="utf-8")
    listed = {row["title"]: row["id"] for row in store.list()}
    assert listed["旧会话"] == "chat-3"
    store.append("chat-3", "user", "只追加消息")
    stored = json.loads(legacy.read_text(encoding="utf-8"))
    assert stored["id"] == "def0b07f8d3d" and stored["session"] == "chat-def0b07f8d3d"
    assert store.load("chat-3")["id"] == "chat-3"
    assert store.load("def0b07f8d3d")["id"] == "chat-3"
    assert store.create("第三次对话")["id"] == "chat-4"


def test_legacy_chat_without_index_reloads_by_readable_id(project):
    entry = layout(project.project_info().path).entry
    store = ChatStore(entry)
    store.root.mkdir(parents=True, exist_ok=True)
    legacy = store.root / "abcdef012345.json"
    legacy.write_text(json.dumps({"id": "abcdef012345", "title": "旧", "messages": []},
                                 ensure_ascii=False), encoding="utf-8")
    assert store.load("chat-1")["title"] == "旧"
    assert store.list()[0]["id"] == "chat-1"


def test_task_ids_are_project_ordinals_and_legacy_audits_map_by_appearance(project):
    entry = layout(project.project_info().path).entry
    root = layout(entry).extraction / "assistant" / "tasks"
    legacy = root / "20261115T120634187839Z-3c5ed8d8"
    legacy.mkdir(parents=True)
    (legacy / "task.json").write_text(json.dumps({"task_id": legacy.name, "status": "completed", "steps": []},
                                                 ensure_ascii=False), encoding="utf-8")
    assert readable_task_id(entry, legacy.name) == "task-1"
    assert next_task_id(entry) == "task-2"
    # 工具返回的可读编号，不是历史目录名。
    summary = tool_api.call("read_task_write_summary", {"task_id": legacy.name}, project=entry)
    assert summary["task_id"] == "task-1"


def test_tools_default_to_the_current_chat_and_task(project, tmp_path):
    entry = layout(project.project_info().path).entry
    chat = ChatStore(entry).create("当前对话")
    with bind_tool_identity(chat_id=chat["id"], task_id="task-9"):
        assert tool_api.call("read_assistant_access", {}, project=entry)["chat_id"] == chat["id"]
        changed = tool_api.call("set_assistant_access", {"access_mode": "full"}, project=entry)
        assert (changed["chat_id"], changed["access_mode"]) == (chat["id"], "full")
        usage = tool_api.call("read_usage_statistics", {}, project=entry)
        assert (usage["chat_id"], usage["task_id"]) == (chat["id"], "task-9")
        source = tmp_path / "资料.txt"
        source.write_text("用户选择的资料。", encoding="utf-8")
        imported = tool_api.call("import_chat_attachments", {"paths": [str(source)]}, project=entry)
        assert imported["chat_id"] == chat["id"]
        listed = tool_api.call("list_chat_attachments", {}, project=entry)
        assert listed["chat_id"] == chat["id"] and len(listed["attachments"]) == 1
        assert listed["attachments"][0]["id"].startswith("attachment-")


def test_identity_parameters_are_optional_instead_of_required():
    optional = {
        "read_assistant_access": ["chat_id"],
        "set_assistant_access": ["chat_id"],
        "import_chat_attachments": ["chat_id", "task_id"],
        "list_chat_attachments": ["chat_id"],
        "read_chat_attachment": ["chat_id"],
        "read_task_write_summary": ["task_id", "chat_id"],
        "read_usage_statistics": ["chat_id", "task_id"],
    }
    for name, keys in optional.items():
        schema = tool_api.TOOLS[name].schema()
        assert not set(schema.get("required") or []) & set(keys), name


def test_tool_schemas_never_publish_a_hash_like_identity():
    for name, tool in tool_api.TOOLS.items():
        published = json.dumps(tool.schema(), ensure_ascii=False)
        assert HEX_TOKEN.search(published) is None, f"{name} 的工具 schema 里出现了 hash 形态的编号"


def test_tool_errors_keep_only_the_category_and_readable_id(project):
    entry = layout(project.project_info().path).entry
    messages = []
    for name, args in (
        ("read_assistant_access", {"chat_id": "chat-99"}),
        ("read_chat_attachment", {"chat_id": "chat-99", "attachment_id": "attachment-9"}),
        ("read_task_write_summary", {"task_id": "20261115T120634187839Z-3c5ed8d8"}),
        ("read_chat_attachment", {"chat_id": "def0b07f8d3d", "attachment_id": "attachment-9"}),
    ):
        with pytest.raises(tool_api.ToolError) as caught:
            tool_api.call(name, args, project=entry)
        messages.append(str(caught.value))
    for message in messages:
        assert_model_safe(message)


def test_assistant_step_errors_are_model_safe(project):
    entry = layout(project.project_info().path).entry
    step = assistant._call(entry, "read_host_file", {"path": r"C:\a\wk\n\narrative-adapt\.runtime\secrets.json"},
                           access_mode="standard")
    assert step["ok"] is False
    assert_model_safe(step["error"])


def test_task_tool_host_error_never_forwards_internal_paths():
    def explode(name, args):
        raise OSError(r"C:\a\wk\n\narrative-adapt\.runtime\projects\p.nproj\chats\def0b07f8d3d"
                      r"\turns\20261115T120634187839Z-3c5ed8d8\metadata.json 不可读")

    server = TaskToolHost([{"name": "read_source", "parameters": {"type": "object"}}], explode)
    address = urlsplit(server.config["url"])
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=60)
    try:
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + server.token}
        connection.request("POST", "/call", json.dumps({"name": "read_source", "args": {}}), headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
        server.close()
    assert payload["ok"] is False
    assert_model_safe(payload["error"])
    assert "[" in payload["error"]  # 类别标签说明了「哪一类」，不是把整句丢掉


def test_scrub_maps_the_reported_diagnostic_to_a_category():
    raw = (r"诊断：C:\a\wk\n\narrative-adapt\.runtime\projects\demo.nproj\chats\def0b07f8d3d"
           r"\turns\20261115T120634187839Z-3c5ed8d8\metadata.json，本次任务未全部完成。")
    cleaned = scrub(raw)
    assert_model_safe(cleaned)
    assert cleaned.startswith("诊断：[") and cleaned.endswith("本次任务未全部完成。")
    assert scrub("会话不存在：def0b07f8d3d") == "会话不存在：[编号]"
    assert scrub("没有路径的一句中文。") == "没有路径的一句中文。"
