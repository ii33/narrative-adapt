"""固定会话模板：系统提示词与工具集在创建时冻结，路由按页面走。"""
import hashlib
import json

import pytest

from narrative import chat_templates
from narrative.chat_store import ChatStore
from narrative.operations import Operations
from narrative.prompt_defaults import DEFAULT_PROMPTS
from narrative.tool_loading import bind_tool_loader, load_tools


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    return Operations.create_graph_project(tmp_path / "templates.nproj")


def store(ops):
    return ChatStore(ops.project_info().path)


def tool_hash(names):
    return hashlib.sha256(json.dumps(list(names), ensure_ascii=False).encode()).hexdigest()


def test_template_chat_freezes_prompt_and_tools(ops):
    """从模板建会话：系统提示词与工具名单就是模板当时的快照。"""
    frozen = chat_templates.snapshot("extract")
    chat = store(ops).create("提取会话", template=frozen)
    saved = store(ops).template(chat["id"])
    assert saved["system_prompt"] == chat_templates.compose_system_prompt("extract")
    assert saved["tools"] == chat_templates.template_tool_names("extract")
    assert 30 <= len(saved["tools"]) < 60
    # 组合的是既有条目，不是新文案：system 段就是 prompt_defaults 里的原文。
    assert DEFAULT_PROMPTS["system"]["text"].strip() in saved["system_prompt"]
    assert DEFAULT_PROMPTS["extraction"]["text"].strip() in saved["system_prompt"]


def test_tools_are_byte_identical_across_three_turns(ops):
    """连续三轮发送，tools 数组逐轮完全一致（提示词缓存不再被中途增长打断）。"""
    frozen = chat_templates.snapshot("extract")
    chat = store(ops).create("三轮", template=frozen)
    allowed = set(frozen["tools"])
    hashes = []
    for _ in range(3):
        visible = chat_templates.visible_tool_names(store(ops).template(chat["id"]), allowed)
        hashes.append(tool_hash(visible))
    assert len(set(hashes)) == 1
    assert tool_hash(frozen["tools"]) == hashes[0]


def test_editing_the_template_does_not_touch_an_existing_chat(ops, monkeypatch):
    """会话进行中改模板：已建会话的提示词与工具集不变。"""
    chat = store(ops).create("已建会话", template=chat_templates.snapshot("extract"))
    before = store(ops).template(chat["id"])
    monkeypatch.setitem(chat_templates.TEMPLATE_FAMILIES, "extract", ("extraction",))
    monkeypatch.setitem(chat_templates.TEMPLATE_PROMPT_KEYS, "extract", ("system", "extraction"))
    changed = chat_templates.snapshot("extract")
    assert changed["tools"] != before["tools"] or changed["system_prompt"] != before["system_prompt"]
    after = store(ops).template(chat["id"])
    assert after == before


def test_load_tools_is_refused_inside_a_frozen_session(ops):
    """冻结会话里 load_tools 不改变工具数组，而是回一条明确提示。"""
    frozen = chat_templates.snapshot("extract")
    visible = list(frozen["tools"])
    seen = {}

    def loader(requested):
        seen["grew"] = True
        return list(visible) + list(requested)

    with bind_tool_loader(loader, visible_names=lambda: visible, allowed_names=set(visible) | {"read_screenplay"},
                          template_frozen=True):
        with pytest.raises(ValueError, match="本会话工具集在创建时固定"):
            load_tools(names=["read_screenplay"], ops=ops)
    assert "grew" not in seen


def test_page_routes_cover_the_three_workbench_pages():
    """原文页→提取、剧本页→剧本、分镜页→分镜；不认识的页面返回 None（用当前会话）。"""
    assert chat_templates.route_for_page("source") == "extract"
    assert chat_templates.route_for_page("browse") == "extract"
    assert chat_templates.route_for_page("screenplay") == "screenplay"
    assert chat_templates.route_for_page("storyboard") == "storyboard"
    assert chat_templates.route_for_page("nowhere") is None


def test_override_applies_to_the_next_send_then_expires():
    """临时覆盖只作用于本页下一次发送；切页回到自动路由。"""
    router = chat_templates.Router()
    assert router.route("screenplay") == "screenplay"
    router.override("screenplay", "storyboard")
    assert router.route("screenplay") == "storyboard"
    assert router.consume("screenplay") == "storyboard"
    # 覆盖已被消费：同一页回到默认路由，别的页也不受影响。
    assert router.route("screenplay") == "screenplay"
    assert router.route("storyboard") == "storyboard"
    with pytest.raises(ValueError):
        router.override("screenplay", "not-a-template")
