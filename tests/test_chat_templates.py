"""固定会话模板：系统提示词与工具集在创建时冻结，路由按页面走。"""
import hashlib
import json

import pytest

from narrative import chat_templates, tool_api
from narrative.chat_store import ChatStore
from narrative.operations import Operations
from narrative.prompt_defaults import DEFAULT_PROMPTS
from narrative.tool_loading import bind_tool_loader, load_tools


@pytest.fixture(autouse=True)
def _isolate_app_templates(tmp_path, monkeypatch):
    """本文件的每个用例都把应用级模板文件指到 tmp：绝不碰真实 ~/.narrative。

    xdist 下 14 个 worker 并行，若共用用户目录同一个文件，原子替换会互相打架，
    还可能把测试写的模板留进用户真实配置里。技能文件同理：模板拼接现在会读它
    （`replaces` 可以顶替某段提示词），不隔离就会读到开发机上的真实技能。
    """
    monkeypatch.setenv("NARRATIVE_CHAT_TEMPLATES_FILE", str(tmp_path / "app-templates.json"))
    monkeypatch.setenv("NARRATIVE_SKILLS_FILE", str(tmp_path / "skills.json"))


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


def test_template_tool_lists_carry_the_skill_group(ops):
    """默认页面走模板会话：冻结名单里要有技能管理工具和常驻的 load_skill。

    没有这一组，界面上就列不出、建不了技能——技能管理在默认页面等于不存在。
    """
    from narrative.skills import LOAD_TOOL_NAME, MANAGE_TOOL_NAMES

    for template_id in ("extract", "screenplay", "storyboard"):
        frozen = chat_templates.snapshot(template_id)
        names = set(frozen["tools"])
        assert set(MANAGE_TOOL_NAMES) <= names, template_id
        assert LOAD_TOOL_NAME in names, template_id
        # 技能组带进来的是这几个，不是整张注册表；配置类工具一个都不许混进来
        assert not any(tool_api.TOOLS[name].scope == "configuration" for name in names), template_id
    assert "skill" in chat_templates.TEMPLATE_FAMILIES["screenplay"]


def test_load_skill_in_a_frozen_template_session_says_what_happened(ops, tmp_path, monkeypatch):
    """模板会话里 load_skill 可见：只给说明的技能照常生效，要开工具的技能必须
    失败并说清原因——冻结的是工具数组，不能把失败说成成功。"""
    from narrative import skills

    monkeypatch.setenv("NARRATIVE_SKILLS_FILE", str(tmp_path / "skills.json"))
    skills.create_skill("只给说明", "不打开工具", "先把范围说清楚再动手。")
    skills.create_skill("要开属性工具", "核对属性", "先 audit_properties 再动手。", families=["properties"])
    frozen = chat_templates.snapshot("screenplay")
    visible = list(frozen["tools"])
    allowed = [name for name, tool in tool_api.TOOLS.items() if tool.scope == "project"]
    with bind_tool_loader(lambda names: list(visible) + list(names), visible_names=lambda: visible,
                          allowed_names=allowed, template_frozen=True):
        result = tool_api.TOOLS["load_skill"].handler(ops, {"name": "只给说明"})
        assert result["runtime_loaded"] is None and result["loaded_tools"] == []
        assert "先把范围说清楚" in result["instructions"]
        with pytest.raises(tool_api.ToolError, match="工具集在创建时固定"):
            tool_api.TOOLS["load_skill"].handler(ops, {"name": "要开属性工具"})
    assert "list_skills" in visible and "load_skill" in visible


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


# ---------------------------------------------------------------- 阶段 2：助手编辑模板
def save(ops, **kwargs):
    from narrative.chat_templates import save_custom, validate

    scope = kwargs.pop("scope")
    payload = validate(kwargs)
    save_custom(scope, payload["id"], payload, ops)
    return payload


CUSTOM = {"id": "screenplay-fast", "name": "剧本快写", "description": "只写正文",
          "tools": ["read_source", "submit_script_unit"], "prompt_keys": ["system", "writing"]}


def test_app_scope_template_is_listed(ops):
    saved = save(ops, scope="app", **CUSTOM)
    rows = {row["id"]: row for row in chat_templates.list_all(ops)}
    assert rows["screenplay-fast"]["scope"] == "app"
    assert rows["screenplay-fast"]["tool_count"] == 2
    assert saved["system_prompt"].startswith(DEFAULT_PROMPTS["system"]["text"].strip()[:20])


def test_project_scope_overrides_the_same_app_template(ops):
    save(ops, scope="app", **CUSTOM)
    save(ops, scope="project", **{**CUSTOM, "name": "工程里的剧本快写"})
    resolved = chat_templates.resolve("screenplay-fast", ops)
    assert resolved["scope"] == "project" and resolved["name"] == "工程里的剧本快写"
    rows = [row for row in chat_templates.list_all(ops) if row["id"] == "screenplay-fast"]
    assert len(rows) == 1 and rows[0]["scope"] == "project"


def test_builtins_cannot_be_deleted_or_overwritten(ops):
    with pytest.raises(ValueError, match="内置模板不可覆盖"):
        save(ops, scope="app", **{**CUSTOM, "id": "extract"})
    from narrative.chat_templates import delete_custom

    with pytest.raises(ValueError, match="内置模板不可删除"):
        delete_custom("app", "extract", ops)
    # 内置仍然只读可用
    assert chat_templates.resolve("extract", ops)["builtin"] is True


def test_unknown_tool_names_are_rejected_and_listed(ops):
    with pytest.raises(ValueError, match="不存在的工具.*read_nothing"):
        save(ops, scope="app", **{**CUSTOM, "tools": ["read_source", "read_nothing"]})


def test_unknown_prompt_key_is_rejected(ops):
    with pytest.raises(ValueError, match="不存在的提示词条目.*no_such_prompt"):
        save(ops, scope="app", **{**CUSTOM, "prompt_keys": ["system", "no_such_prompt"]})
    # 自由文本形态允许：不需要 prompt_keys。
    assert save(ops, scope="app", **{**CUSTOM, "id": "free-text", "prompt_keys": None,
                                     "system_prompt": "你是快写助手。"})["system_prompt"] == "你是快写助手。"


def test_chat_built_from_a_custom_template_freezes_its_prompt_and_tools(ops):
    saved = save(ops, scope="app", **CUSTOM)
    chat = store(ops).create("自定义会话", template=chat_templates.snapshot("screenplay-fast", ops))
    frozen = store(ops).template(chat["id"])
    assert frozen["system_prompt"] == saved["system_prompt"]
    assert frozen["tools"] == saved["tools"]
    # 改了自定义模板，已建会话仍不变（冻结语义对自定义模板同样成立）。
    save(ops, scope="app", **{**CUSTOM, "tools": ["read_source"]})
    assert store(ops).template(chat["id"]) == frozen


# ------------------------------------------------- 阶段 3：提示词位与来源


def test_builtin_template_prompt_still_joins_the_default_entries():
    """拼接语义没变：还是那几段内置条目按顺序用空行连起来，一字不差。"""
    expected = "\n\n".join(DEFAULT_PROMPTS[key]["text"].strip()
                           for key in chat_templates.TEMPLATE_PROMPT_KEYS["extract"])
    assert chat_templates.compose_system_prompt("extract") == expected


def test_snapshot_carries_prompt_sources_and_freezes_them(ops):
    """快照多带一份 prompt_sources：每段来自内置还是哪条技能、版本多少。"""
    from narrative import skills

    skills.create_skill("我的写法", "按我的顺序写", "先写冲突，再写解决。", replaces="writing")
    frozen = chat_templates.snapshot("screenplay", ops)
    assert frozen["prompt_sources"]["writing"]["source"] == "skill"
    assert frozen["prompt_sources"]["writing"]["name"] == "我的写法"
    assert frozen["prompt_sources"]["writing"]["version"]  # 技能的 updated_at
    assert frozen["prompt_sources"]["system"] == {
        "source": "builtin", "name": None, "version": DEFAULT_PROMPTS["system"]["version"]}
    # 全文跟着换：被替换的段是技能正文，其余段仍是内置原文。
    assert "先写冲突，再写解决。" in frozen["system_prompt"]
    assert DEFAULT_PROMPTS["writing"]["text"].strip() not in frozen["system_prompt"]
    assert DEFAULT_PROMPTS["scene_policy"]["text"].strip() in frozen["system_prompt"]

    # 冻结的是全文与来源：之后删掉技能，已建会话一个字都不变。
    skills.delete_skill("我的写法", confirm=True)
    chat = store(ops).create("冻结来源", template=frozen)
    saved = store(ops).template(chat["id"])
    assert saved["prompt_sources"]["writing"]["name"] == "我的写法"
    assert "先写冲突，再写解决。" in saved["system_prompt"]
    # 新快照回落内置。
    assert chat_templates.snapshot("screenplay", ops)["prompt_sources"]["writing"]["source"] == "builtin"


def test_custom_template_prompt_sources_come_from_the_registry(ops):
    """自定义模板（prompt_keys 形态）也按技能覆盖解析，语义仍是「按顺序拼这几段」。"""
    from narrative import skills

    skills.create_skill("我的写法", "按我的顺序写", "先写冲突，再写解决。", replaces="writing")
    save(ops, scope="app", **CUSTOM)
    resolved = chat_templates.resolve("screenplay-fast", ops)
    assert resolved["prompt_sources"]["writing"]["name"] == "我的写法"
    assert resolved["prompt_sources"]["system"]["source"] == "builtin"
    assert resolved["prompt_versions"]["writing"].startswith("我的写法@")
    assert resolved["system_prompt"] == (DEFAULT_PROMPTS["system"]["text"].strip()
                                        + "\n\n" + "先写冲突，再写解决。")
    frozen = chat_templates.snapshot("screenplay-fast", ops)
    assert frozen["prompt_sources"]["writing"]["source"] == "skill"


def test_free_text_custom_template_has_no_prompt_sources(ops):
    """自由文本形态没有提示词位可言：没有 prompt_keys 就不编一份来源出来。"""
    save(ops, scope="app", **{**CUSTOM, "id": "free-text", "prompt_keys": None,
                              "system_prompt": "你是快写助手。"})
    resolved = chat_templates.resolve("free-text", ops)
    assert resolved["system_prompt"] == "你是快写助手。"
    assert "prompt_sources" not in resolved
