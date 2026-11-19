"""技能：存储健壮性、校验、内置只读，以及 load_skill 的动态加载与冻结拒绝。

所有用例都把 `NARRATIVE_SKILLS_FILE` 指到 tmp_path：绝不碰真实
`~/.narrative/skills.json`（xdist 下多个 worker 会互相覆盖用户配置）。
"""
import json
from pathlib import Path

import pytest

from narrative import skills, tool_api
from narrative.skills import (
    BUILTIN_GENERATION_SKILLS,
    BUILTIN_SKILLS,
    LOAD_TOOL_NAME,
    MANAGE_TOOL_NAMES,
    MAX_INSTRUCTIONS_CHARS,
    SCOPE,
    SKILL_TOOL_NAMES,
    WORKFLOW_KINDS,
    Skill,
    SkillError,
)
from narrative.tool_loading import (
    CORE_TOOL_NAMES,
    FAMILY_DESCRIPTIONS,
    FULL_SCOPES,
    STANDARD_SCOPES,
    bind_tool_loader,
    initial_tool_names,
    list_available_tools,
    resolve_tool_names,
    tool_family,
)


@pytest.fixture(autouse=True)
def skills_file(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    monkeypatch.setenv("NARRATIVE_SKILLS_FILE", str(path))
    return path


@pytest.fixture
def probe_tool(monkeypatch):
    """一个假工具：验证「按名加载」不需要碰任何真实业务组。"""
    fake = tool_api.Tool("skill_probe_tool", "技能测试探针", {},
                         lambda ops, args: {"ok": True}, scope="project")
    monkeypatch.setitem(tool_api.TOOLS, "skill_probe_tool", fake)
    return fake


def call(tool, **args):
    """直接调用工具处理函数（不打开工程，和 test_tool_loading 同一手法）。"""
    return tool_api.TOOLS[tool].handler(None, args)


def project_names():
    return [name for name, tool in tool_api.TOOLS.items() if tool.scope == "project"]


def make_skill(name="试写技能", summary="一句话摘要", instructions="先读数，再写。",
               families=None, tools=None, replaces=None, workflow=None):
    return skills.create_skill(name, summary, instructions, families, tools,
                               replaces=replaces, workflow=workflow)


def bound_loader(allowed, visible, *, refuse=None, frozen=False, calls=None):
    """把 tool_loading 的运行时绑定搭起来，模拟一次对话里的加载器。"""
    def loader(requested):
        if calls is not None:
            calls.append(list(requested))
        if refuse:
            raise ValueError(refuse)
        visible.extend(item for item in requested if item not in visible)
        return list(visible)
    return bind_tool_loader(loader, visible_names=lambda: visible, allowed_names=allowed,
                            template_frozen=frozen)


# ------------------------------------------------------------------ 存储


def test_default_path_follows_env_override_and_home(monkeypatch, tmp_path):
    assert skills.default_path() == tmp_path / "skills.json"
    monkeypatch.delenv("NARRATIVE_SKILLS_FILE")
    assert skills.default_path() == Path.home() / ".narrative" / "skills.json"


def test_round_trip_writes_versioned_json_atomically(skills_file):
    make_skill(families=["sources"], tools=["read_source"])
    payload = json.loads(skills_file.read_text(encoding="utf-8"))
    assert payload["version"] == skills.FILE_VERSION
    assert [row["name"] for row in payload["skills"]] == ["试写技能"]
    assert payload["skills"][0]["families"] == ["sources"]
    assert payload["skills"][0]["builtin"] is False
    assert not (skills_file.parent / (skills_file.name + ".tmp")).exists()
    loaded = skills.get_skill("试写技能")
    assert loaded.families == ["sources"] and loaded.tools == ["read_source"]
    assert loaded.created_at and loaded.updated_at and loaded.created_at == loaded.updated_at


def test_builtin_skills_are_never_written_to_the_user_file(skills_file):
    assert len(skills.load_skills()) == len(BUILTIN_SKILLS)
    make_skill()
    saved = json.loads(skills_file.read_text(encoding="utf-8"))["skills"]
    assert [row["name"] for row in saved] == ["试写技能"]
    assert [skill.name for skill in skills.load_skills()] == [
        *[skill.name for skill in BUILTIN_SKILLS], "试写技能"]


def test_corrupt_file_is_reported_and_left_untouched(skills_file):
    skills_file.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(SkillError) as info:
        skills.load_skills()
    message = str(info.value)
    assert "不是合法 JSON" in message and skills_file.name in message
    assert "原文件没有被改动" in message
    assert skills_file.read_text(encoding="utf-8") == "{ 这不是 JSON"
    assert not (skills_file.parent / (skills_file.name + ".bak")).exists()


def test_save_backs_up_unreadable_file_instead_of_destroying_it(skills_file):
    skills_file.write_text("{ 坏掉的旧文件", encoding="utf-8")
    skills.save_skills([Skill(name="新技能", summary="摘要", instructions="说明")])
    backup = skills_file.parent / (skills_file.name + ".bak")
    assert backup.read_text(encoding="utf-8") == "{ 坏掉的旧文件"
    assert [skill.name for skill in skills.load_custom_skills()] == ["新技能"]


def test_user_file_cannot_forge_a_builtin_skill(skills_file):
    skills_file.write_text(json.dumps({"version": 1, "skills": [
        {"name": "只读冒充", "summary": "s", "instructions": "i", "builtin": True}]},
        ensure_ascii=False), encoding="utf-8")
    loaded = skills.load_custom_skills()[0]
    assert loaded.builtin is False and skills.delete_skill("只读冒充") is True


def test_user_file_cannot_shadow_a_builtin_name(skills_file):
    skills_file.write_text(json.dumps({"version": 1, "skills": [
        {"name": BUILTIN_SKILLS[0].name, "summary": "s", "instructions": "i"}]},
        ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SkillError, match="不能与内置技能同名"):
        skills.load_skills()


# ------------------------------------------------------------------ 校验


def test_duplicate_names_are_rejected_case_insensitively():
    make_skill("Storyboard Check")
    with pytest.raises(SkillError, match="已存在"):
        make_skill("storyboard check")
    with pytest.raises(SkillError, match="内置技能名"):
        make_skill(BUILTIN_SKILLS[0].name)


def test_unknown_family_is_rejected_with_options():
    with pytest.raises(SkillError) as info:
        make_skill(families=["storyboardz"])
    message = str(info.value)
    assert "未知业务组：storyboardz" in message
    assert "screenplay" in message and "storyboard" in message


def test_unknown_tool_is_rejected_with_options():
    with pytest.raises(SkillError) as info:
        make_skill(families=["sources"], tools=["read_sourcez"])
    message = str(info.value)
    assert "未知工具：read_sourcez" in message and "read_source" in message
    with pytest.raises(SkillError, match="是业务组名"):
        make_skill(families=["sources"], tools=["screenplay"])


def test_field_limits_are_enforced():
    with pytest.raises(SkillError, match=str(MAX_INSTRUCTIONS_CHARS)):
        make_skill(instructions="长" * (MAX_INSTRUCTIONS_CHARS + 1))
    make_skill("刚好到上限", instructions="长" * MAX_INSTRUCTIONS_CHARS)
    with pytest.raises(SkillError, match="summary"):
        make_skill("摘要超长", summary="摘" * (skills.MAX_SUMMARY_CHARS + 1))
    with pytest.raises(SkillError, match="不能为空"):
        make_skill("   ")
    with pytest.raises(SkillError, match="换行"):
        make_skill("换行\n名字")


def test_builtin_skills_are_read_only():
    builtin = BUILTIN_SKILLS[0]
    assert skills.get_skill(builtin.name).builtin is True
    with pytest.raises(SkillError, match="只读"):
        skills.update_skill(builtin.name, summary="改摘要")
    with pytest.raises(SkillError, match="不可删除"):
        skills.delete_skill(builtin.name)
    assert skills.get_skill(builtin.name).summary == builtin.summary


def test_builtin_skills_reference_real_tools_and_families():
    known_families = skills.known_families()
    known_tools = skills.known_tools()
    # 内置 = 三条作业说明 + 五条生成工作流（内置生成技能随程序发布，开箱就能跑）。
    assert len(BUILTIN_SKILLS) == 8
    assert len(BUILTIN_GENERATION_SKILLS) == 5
    for skill in BUILTIN_SKILLS:
        assert skill.builtin and skill.enabled
        assert set(skill.families) <= known_families, skill.name
        assert set(skill.tools) <= known_tools, skill.name
        # 正文要写成给模型的作业说明，而不是给用户看的文案。
        assert len(skill.instructions) > 300 and "##" in skill.instructions


def test_builtin_generation_skills_declare_the_documented_workflows():
    """五条内置生成技能的 `workflow` 形状与 docs/SKILLS.md 的表一致。"""
    by_kind = {skill.workflow["kind"]: skill for skill in BUILTIN_GENERATION_SKILLS}
    assert set(by_kind) == {"sketch", "style", "video", "asset_design", "asset_sketch"}
    assert set(by_kind) <= set(WORKFLOW_KINDS)
    for skill in BUILTIN_GENERATION_SKILLS:
        workflow = skill.workflow
        # 生成技能不改提示词：它的正文是给模型看的作业说明，不是提示词替换。
        assert skill.replaces is None, skill.name
        assert isinstance(workflow["model"], str) and workflow["model"].strip()
        # 许可必须写明来源与「商用待确认」这类结论：界面与回执都靠它提醒风险。
        assert "待确认" in workflow["license"], skill.name
        assert isinstance(workflow["expectations"], dict) and workflow["expectations"]

    sketch = by_kind["sketch"].workflow
    assert sketch["model"] == "flux3-image-klein"
    assert sketch["workflow_file"] == "workflows/storyboard_flux_klein.json"
    assert sketch["expectations"] == {"aspect_from_shot": True, "reference_images_max": 10}
    # 相对路径按包目录解析，所以这条文件必须真的在包里：缺了执行器只能记 WORKFLOW_FILE_MISSING。
    assert (Path(skills.__file__).parent / sketch["workflow_file"]).is_file()

    style = by_kind["style"].workflow
    assert style["model"] == "krea-2"
    assert style["expectations"] == {"uses_style_reference": True}
    # 还没有对应的工作流文件就先留空：执行器会记 warning，而不是假装用上了。
    assert "workflow_file" not in style

    video = by_kind["video"].workflow
    assert video["model"] == "MiniMax-H3"
    assert video["expectations"] == {"modes": ["endpoints"], "max_segment_ms": 2000,
                                     "resolution": ["768P", "2K"]}
    # 声明的分辨率必须是视频服务真的接受的：名单在内核里不另写一份，直接核对 provider。
    from narrative import storyboard_video_provider as video_provider

    assert video["model"] in video_provider.MODELS
    for resolution in video["expectations"]["resolution"]:
        video_provider.validate_options(video["model"], resolution, 5000)  # 不抛即合法


def test_builtin_generation_skills_are_read_only_and_cannot_be_shadowed():
    for skill in BUILTIN_GENERATION_SKILLS:
        assert skills.get_skill(skill.name).builtin is True
        with pytest.raises(SkillError, match="只读"):
            skills.update_skill(skill.name, summary="改摘要")
        with pytest.raises(SkillError, match="不可删除"):
            skills.delete_skill(skill.name, confirm=True)
        # 用户不能另建一条同名技能顶替内置：否则「删哪一条」就说不清了。
        with pytest.raises(SkillError, match="内置技能名"):
            make_skill(skill.name, workflow={"kind": skill.workflow["kind"], "model": "m"})


# -------------------------------------------------------------- 工具链路


def test_create_list_read_update_delete_through_tools():
    created = call("create_skill", name="场次核对", summary="核对场表顺序",
                   instructions="先用read_scene_table_context。", families=["writing"])
    assert created["created"]["enabled"] is True and created["created"]["builtin"] is False
    assert "read_scene_table_context" in created["opens"]["resolved_tools"]

    listed = call("list_skills")
    names = [row["name"] for row in listed["skills"]]
    assert {"场次核对", *(skill.name for skill in BUILTIN_SKILLS)} <= set(names)
    assert listed["count"] == len(names)
    assert all("instructions" not in row for row in listed["skills"])

    read = call("read_skill", name="场次核对")
    assert read["skill"]["instructions"] == "先用read_scene_table_context。"
    assert read["skill"]["families"] == ["writing"]
    assert "read_scene_table_context" in read["opens"]["resolved_tools"]

    updated = call("update_skill", name="场次核对", summary="只改摘要")
    assert updated["updated"]["summary"] == "只改摘要"
    assert updated["updated"]["families"] == ["writing"]  # 没传的字段不动
    assert updated["updated"]["instructions"] == read["skill"]["instructions"]

    with pytest.raises(tool_api.ToolError, match="confirm=true"):
        call("delete_skill", name="场次核对", confirm=False)
    assert call("delete_skill", name="场次核对", confirm=True)["deleted"] is True
    assert "场次核对" not in [row["name"] for row in call("list_skills")["skills"]]
    with pytest.raises(tool_api.ToolError, match="没有自定义技能"):
        call("delete_skill", name="场次核对", confirm=True)


def test_tool_errors_are_model_facing_and_builtin_delete_explains_why():
    with pytest.raises(tool_api.ToolError) as info:
        call("delete_skill", name=BUILTIN_SKILLS[1].name, confirm=True)
    message = str(info.value)
    assert "内置技能" in message and "不可删除" in message and "随程序发布" in message
    with pytest.raises(tool_api.ToolError, match="没有技能"):
        call("read_skill", name="不存在的技能")
    with pytest.raises(tool_api.ToolError, match="请给出技能名字"):
        call("load_skill", name="   ")


def test_skill_tools_are_registered_with_expected_scope_and_writes():
    assert tool_api.SKILL_STARTUP_ERROR is None
    for name in SKILL_TOOL_NAMES:
        assert name in tool_api.TOOLS, name
        assert tool_family(name) == "skill"
    # 五个管理工具归 skill scope：协作者能建立、编辑、管理技能，但它们不是
    # configuration——密钥与会话模板写入仍然不可由模型调用。
    assert SCOPE == "skill" and SCOPE != "configuration"
    for name in MANAGE_TOOL_NAMES:
        assert tool_api.TOOLS[name].scope == SCOPE, name
    assert tool_api.TOOLS[LOAD_TOOL_NAME].scope == "project"
    writes = {name for name in SKILL_TOOL_NAMES if tool_api.TOOLS[name].writes}
    assert writes == {"create_skill", "update_skill", "delete_skill"}
    assert "skill" in FAMILY_DESCRIPTIONS
    # 既有分组一个没被改动。
    assert tool_family("read_source") == "sources"
    assert tool_family("apply_storyboard_changes") == "storyboard"


def test_skill_scope_is_standard_but_configuration_stays_closed():
    """skill 进标准模式；configuration 两档都不进（这是设计红线）。"""
    assert SCOPE in STANDARD_SCOPES
    assert "configuration" not in STANDARD_SCOPES and "configuration" not in FULL_SCOPES
    # 完全访问必须是标准模式的超集
    assert STANDARD_SCOPES < FULL_SCOPES
    # 标准模式就能按业务组解析出全部六个技能工具，不需要完全访问
    resolved = resolve_tool_names(families=["skill"])
    assert set(resolved) == set(SKILL_TOOL_NAMES)
    # 密钥、模板写入依旧解析不了
    for name in ("configure_project_api_key", "save_chat_template"):
        with pytest.raises(ValueError, match="权限"):
            resolve_tool_names(names=[name], access_mode="full")


def test_load_skill_is_a_resident_tool():
    """load_skill 常驻：模型一步就能加载技能，不必先 list_available_tools → load_tools。"""
    assert LOAD_TOOL_NAME in CORE_TOOL_NAMES
    assert LOAD_TOOL_NAME in initial_tool_names(project_names())
    # 其余五个管理工具不常驻（要靠 skill 组带入），否则每场会话都多五个合同
    assert not (MANAGE_TOOL_NAMES & set(CORE_TOOL_NAMES))


def test_missing_skills_file_keeps_discovery_and_other_families_unchanged(skills_file):
    assert not skills_file.exists()
    rows = skills.load_skills()
    assert [skill.name for skill in rows] == [skill.name for skill in BUILTIN_SKILLS]
    assert not skills_file.exists()  # 读不写：没有技能文件时行为与改动前一致
    families = {group["family"] for group in list_available_tools()["families"]}
    assert "skill" in families and "screenplay" in families
    # 技能组现在整组可见（管理工具归 skill scope）：没有技能文件时也一样，
    # 因为它们是代码注册的，不依赖用户配置。
    assert [row["name"] for row in list_available_tools(family="skill")["tools"]] == [
        "list_skills", "read_skill", "list_prompt_keys",
        "create_skill", "update_skill", "delete_skill", "load_skill"]
    # 其它业务组没被这组带跑：读取类工具仍归自己那一组
    assert tool_family("read_source") == "sources" and tool_family("submit_scene_table") == "writing"


# -------------------------------------------------------------- load_skill


def test_load_skill_opens_family_and_named_tool(probe_tool):
    make_skill("读原文练习", "按范围读原文", "先 list_sources 再 read_source_slice。",
               families=["sources"], tools=["skill_probe_tool"])
    allowed = project_names()
    visible = initial_tool_names(allowed)
    calls = []
    with bound_loader(allowed, visible, calls=calls):
        result = call("load_skill", name="读原文练习")
        assert result["instructions"] == "先 list_sources 再 read_source_slice。"
        assert {"read_source", "list_sources", "skill_probe_tool"} <= set(result["resolved_tools"])
        assert result["runtime_loaded"] is True
        assert "read_source" in result["loaded_tools"]
        assert "skill_probe_tool" in result["newly_loaded_tools"]
        # 核心常驻工具（read_source 等）不重复计入「新加载」，非核心的 source 工具才是真的被打开。
        assert "import_source" in result["newly_loaded_tools"]
        assert set(result["newly_loaded_tools"]) <= set(result["resolved_tools"])
        assert calls == [result["resolved_tools"]]
        assert list_available_tools(names=["import_source"])["tools"][0]["loaded"] is True
        # 同一会话再加载一次：不重复增长工具数组。
        calls.clear()
        again = call("load_skill", name="读原文练习")
        assert again["newly_loaded_tools"] == [] and calls == []


def test_load_skill_reuses_tools_that_are_already_visible():
    make_skill("发现练习", "先发现再加载", "用 list_available_tools。",
               tools=["list_available_tools", "load_tools"])
    allowed = project_names()
    visible = initial_tool_names(allowed)
    calls = []
    with bound_loader(allowed, visible, calls=calls):
        result = call("load_skill", name="发现练习")
        assert result["newly_loaded_tools"] == [] and calls == []
        assert result["runtime_loaded"] is True


def test_load_skill_fails_clearly_when_the_session_is_frozen():
    make_skill("分场练习", "排场表", "先 read_screenplay。", families=["screenplay"])
    allowed = project_names()
    visible = initial_tool_names(allowed)
    with bound_loader(allowed, visible, frozen=True):
        with pytest.raises(tool_api.ToolError) as info:
            call("load_skill", name="分场练习")
    message = str(info.value)
    assert "没能生效" in message and "在创建时固定" in message
    assert "新建一个" in message and "只给说明" in message
    # 冻结会话的工具组一个都没被悄悄打开。
    assert "read_screenplay" not in visible


def test_load_skill_fails_clearly_when_the_runtime_refuses_to_grow():
    """真实运行时（conversation_runtime._load_tools）在模板会话里抛的是这句。"""
    make_skill("分场练习", "排场表", "先 read_screenplay。", families=["screenplay"])
    allowed = project_names()
    visible = initial_tool_names(allowed)
    with bound_loader(allowed, visible, refuse="本会话工具集在创建时固定；需要更多工具请新建会话"):
        with pytest.raises(tool_api.ToolError, match="没能生效"):
            call("load_skill", name="分场练习")


def test_instructions_only_skill_still_works_in_a_frozen_session():
    make_skill("只给说明", "不打开工具", "先把范围说清楚再动手。")
    allowed = project_names()
    visible = initial_tool_names(allowed)
    with bound_loader(allowed, visible, frozen=True):
        result = call("load_skill", name="只给说明")
    assert result["resolved_tools"] == [] and result["runtime_loaded"] is None
    assert result["instructions"] == "先把范围说清楚再动手。"


def test_load_skill_without_a_runtime_returns_contracts_not_a_false_success():
    make_skill("读原文练习", "按范围读原文", "先 list_sources。", families=["sources"])
    result = call("load_skill", name="读原文练习")
    assert result["runtime_loaded"] is False
    assert result["loaded_tools"] == []
    assert result["tool_contracts"] and result["tool_contracts"][0]["inputSchema"]
    assert "无法在这里激活" in result["message"]


def test_load_skill_refuses_disabled_and_unknown_skills():
    make_skill("停用技能")
    skills.update_skill("停用技能", enabled=False)
    with pytest.raises(tool_api.ToolError, match="已停用"):
        call("load_skill", name="停用技能")
    with pytest.raises(tool_api.ToolError, match="没有技能"):
        call("load_skill", name="不存在")
    skills.update_skill("停用技能", enabled=True)
    assert call("read_skill", name="停用技能")["skill"]["enabled"] is True


# --------------------------------------------- 替换提示词位与工作流声明


def test_replaces_accepts_a_known_prompt_key():
    from narrative.prompt_defaults import DEFAULT_PROMPTS

    skill = make_skill("改写提取要求", replaces="extraction")
    assert skill.replaces == "extraction"
    assert skills.get_skill("改写提取要求").replaces == "extraction"
    # 白名单就是内置提示词表：新增一段提示词不必改技能模块。
    assert skill.replaces in DEFAULT_PROMPTS


def test_unknown_replaces_key_is_rejected_with_options():
    with pytest.raises(SkillError) as info:
        make_skill("拼错键", replaces="extractionz")
    message = str(info.value)
    assert "未知提示词条目：extractionz" in message
    # 报错是给用户看的：可选键要列出来，并说清「留空 = 只附加说明」。
    assert "extraction" in message and "writing" in message
    assert "只附加说明" in message


def test_hand_edited_file_with_unknown_replaces_is_rejected(skills_file):
    """手改文件走同一套校验：未知的 replaces 当场报错，不留到解析提示词时才对不上。"""
    skills_file.write_text(json.dumps({"version": 1, "skills": [
        {"name": "手改", "summary": "s", "instructions": "i", "replaces": "no_such"}]},
        ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SkillError, match="未知提示词条目：no_such"):
        skills.load_custom_skills()


def test_empty_replaces_means_instructions_only():
    """replaces 为空就是纯附加说明（既有语义）；update 传空串表示改回纯说明。"""
    assert make_skill("纯说明").replaces is None
    assert skills.update_skill("纯说明", replaces="writing").replaces == "writing"
    assert skills.update_skill("纯说明", replaces="").replaces is None
    # 没传 replaces 时不动它（局部更新语义与 families/tools 一致）。
    assert skills.update_skill("纯说明", replaces="scene_policy").replaces == "scene_policy"
    assert skills.update_skill("纯说明", summary="只改摘要").replaces == "scene_policy"


def test_workflow_shape_is_kept_and_kind_is_whitelisted():
    workflow = {"kind": "sketch", "model": "local/sketch-v1", "license": "CC-BY-4.0",
                "workflow_file": "workflows/sketch.json", "params": {"steps": 20},
                "expectations": {"min_side": 768}, "note": "不认识的键宽松保留"}
    skill = make_skill("素描工作流", workflow=workflow)
    assert skill.workflow["kind"] == "sketch"
    assert skill.workflow["params"] == {"steps": 20}
    assert skill.workflow["note"] == "不认识的键宽松保留"
    assert skills.get_skill("素描工作流").workflow["workflow_file"] == "workflows/sketch.json"

    with pytest.raises(SkillError) as info:
        make_skill("未知工作流", workflow={"kind": "sketchz", "model": "m"})
    message = str(info.value)
    assert "workflow.kind 未知：sketchz" in message
    assert all(kind in message for kind in ("sketch", "style", "video"))


def test_workflow_field_shapes_are_checked():
    with pytest.raises(SkillError, match="workflow.model 须为非空文本"):
        make_skill("缺模型", workflow={"kind": "style"})
    with pytest.raises(SkillError, match="workflow.params 须为 JSON 对象"):
        make_skill("坏参数", workflow={"kind": "video", "model": "m", "params": [1]})
    with pytest.raises(SkillError, match="workflow.license 须为文本"):
        make_skill("坏许可", workflow={"kind": "video", "model": "m", "license": 1})
    with pytest.raises(SkillError, match="workflow 须为 JSON 对象"):
        make_skill("坏形状", workflow=["sketch"])


def test_update_skill_clears_workflow_with_an_empty_object():
    make_skill("工作流", workflow={"kind": "video", "model": "m"})
    assert skills.update_skill("工作流", summary="改摘要").workflow == {"kind": "video", "model": "m"}
    assert skills.update_skill("工作流", workflow={}).workflow is None


def test_list_skills_reports_replaces_and_workflow():
    make_skill("替换写法", replaces="writing", workflow={"kind": "style", "model": "m"})
    rows = {row["name"]: row for row in call("list_skills")["skills"]}
    assert rows["替换写法"]["replaces"] == "writing"
    assert rows["替换写法"]["workflow"] == {"kind": "style", "model": "m"}
    # 内置技能两个字段都是空：它们不改提示词、也不声明工作流。
    builtin = BUILTIN_SKILLS[0].name
    assert rows[builtin]["replaces"] is None and rows[builtin]["workflow"] is None
    read = call("read_skill", name="替换写法")["skill"]
    assert read["replaces"] == "writing" and read["workflow"]["kind"] == "style"


def test_list_prompt_keys_shows_who_replaces_each_prompt():
    from narrative.prompt_defaults import DEFAULT_PROMPTS

    listing = call("list_prompt_keys")
    assert listing["count"] == len(DEFAULT_PROMPTS)
    assert all(row["replaced_by"] is None for row in listing["keys"])
    # 只读工具：不写盘、不标 writes。
    assert tool_api.TOOLS["list_prompt_keys"].writes is False
    assert tool_api.TOOLS["list_prompt_keys"].scope == SCOPE

    call("create_skill", name="改写写作要求", summary="摘要", instructions="按我的顺序写。",
         replaces="writing")
    rows = {row["key"]: row for row in call("list_prompt_keys")["keys"]}
    assert rows["writing"]["replaced_by"] == "改写写作要求"
    assert rows["writing"]["source"] == "skill"
    assert rows["system"]["replaced_by"] is None and rows["system"]["source"] == "builtin"
