"""提示词注册表：技能覆盖内置提示词位、冲突取最近更新、坏文件回落内置。

所有用例都把 `NARRATIVE_SKILLS_FILE` 指到 tmp_path：绝不碰真实
`~/.narrative/skills.json`（xdist 下多个 worker 会互相覆盖用户配置）。
"""
import json

import pytest

from narrative import chat_templates, prompt_registry, skills
from narrative.prompt_defaults import DEFAULT_PROMPTS


@pytest.fixture(autouse=True)
def skills_file(tmp_path, monkeypatch):
    path = tmp_path / "skills.json"
    monkeypatch.setenv("NARRATIVE_SKILLS_FILE", str(path))
    # 模板侧也会读技能文件（拼接走注册表），应用级模板文件同样隔离到 tmp。
    monkeypatch.setenv("NARRATIVE_CHAT_TEMPLATES_FILE", str(tmp_path / "app-templates.json"))
    return path


def write_rows(path, *rows):
    """直接写文件而不是走 create_skill：需要精确控制 `updated_at`。

    「最近更新优先」靠这个时间戳判定，而 `create_skill` 用的是秒级 `now()`——
    同一秒内建两条技能就测不出先后了。
    """
    path.write_text(json.dumps({"version": 1, "skills": list(rows)}, ensure_ascii=False),
                    encoding="utf-8")


def row(name, *, instructions="这条技能要求的做法。", replaces=None, enabled=True,
        updated_at="2026-01-01T00:00:00Z", **extra):
    data = {"name": name, "summary": "一句话摘要", "instructions": instructions,
            "enabled": enabled, "created_at": updated_at, "updated_at": updated_at}
    if replaces:
        data["replaces"] = replaces
    data.update(extra)
    return data


def builtin_text(key):
    return DEFAULT_PROMPTS[key]["text"].strip()


# ---------------------------------------------------------------- 解析优先级


def test_builtin_prompt_is_used_when_no_skill_replaces_it():
    resolved = prompt_registry.resolve_prompt("extraction")
    assert resolved["source"] == prompt_registry.BUILTIN
    assert resolved["overridden_by"] is None and resolved["name"] is None
    assert resolved["text"] == builtin_text("extraction")
    assert resolved["version"] == str(DEFAULT_PROMPTS["extraction"]["version"])
    assert "ignored" not in resolved and "problem" not in resolved


def test_skill_replaces_the_builtin_prompt(skills_file):
    write_rows(skills_file, row("我的提取要求", replaces="extraction",
                                instructions="只写原文能指认的事实。"))
    resolved = prompt_registry.resolve_prompt("extraction")
    assert resolved["source"] == prompt_registry.SKILL
    assert resolved["overridden_by"] == "我的提取要求" and resolved["name"] == "我的提取要求"
    assert resolved["text"] == "只写原文能指认的事实。"
    assert resolved["version"] == "2026-01-01T00:00:00Z"
    # 内置文本一个字符都没动：提示词快照的纪律（改文本必须升版本）只针对内置条目。
    assert DEFAULT_PROMPTS["extraction"]["text"].strip() == builtin_text("extraction")


def test_skill_replacement_reaches_the_template_prompt(skills_file):
    """模板拼接走注册表：被替换的段换成技能正文，其余段仍是内置原文。"""
    write_rows(skills_file, row("我的提取要求", replaces="extraction",
                                instructions="只写原文能指认的事实。"))
    prompt = chat_templates.compose_system_prompt("extract")
    assert "只写原文能指认的事实。" in prompt
    assert builtin_text("extraction") not in prompt
    assert builtin_text("system") in prompt and builtin_text("granularity_fine") in prompt


def test_newest_skill_wins_and_the_ignored_one_is_reported(skills_file):
    """同一提示词位允许多条技能：解析取最近更新，并把被忽略的名单回报出来。"""
    write_rows(skills_file,
               row("旧改法", replaces="writing", instructions="旧的写法要求。",
                   updated_at="2026-01-01T00:00:00Z"),
               row("新改法", replaces="writing", instructions="新的写法要求。",
                   updated_at="2026-02-02T00:00:00Z"))
    resolved = prompt_registry.resolve_prompt("writing")
    assert resolved["overridden_by"] == "新改法"
    assert resolved["text"] == "新的写法要求。"
    assert resolved["ignored"] == ["旧改法"]
    keys = {item["key"]: item for item in prompt_registry.list_prompt_keys()}
    assert keys["writing"]["replaced_by"] == "新改法"


def test_duplicate_replaces_is_saved_not_rejected(skills_file):
    """保存不拒绝同键多条（用户可以先备几条比较），冲突只在解析时按时间裁决。"""
    skills.create_skill("第一条", "摘要", "第一条的正文。", replaces="writing")
    skills.create_skill("第二条", "摘要", "第二条的正文。", replaces="writing")
    assert [skill.name for skill in skills.load_custom_skills()] == ["第一条", "第二条"]
    resolved = prompt_registry.resolve_prompt("writing")
    assert len(resolved["ignored"]) == 1
    assert {resolved["overridden_by"], resolved["ignored"][0]} == {"第一条", "第二条"}


def test_disabled_skill_falls_back_to_builtin(skills_file):
    write_rows(skills_file, row("停用的改法", replaces="writing", enabled=False))
    resolved = prompt_registry.resolve_prompt("writing")
    assert resolved["source"] == prompt_registry.BUILTIN
    assert resolved["text"] == builtin_text("writing")
    assert resolved["overridden_by"] is None


def test_deleted_skill_falls_back_to_builtin(skills_file):
    skills.create_skill("临时改法", "摘要", "临时正文。", replaces="scene_policy")
    assert prompt_registry.resolve_prompt("scene_policy")["overridden_by"] == "临时改法"
    skills.delete_skill("临时改法", confirm=True)
    resolved = prompt_registry.resolve_prompt("scene_policy")
    assert resolved["source"] == prompt_registry.BUILTIN and resolved["overridden_by"] is None
    assert resolved["text"] == builtin_text("scene_policy")


# -------------------------------------------------------------------- compose


def test_compose_keeps_order_and_reports_sources(skills_file):
    write_rows(skills_file, row("我的写法", replaces="writing", instructions="按我的顺序写。"))
    composed = prompt_registry.compose(["system", "writing", "scene_policy"])
    assert composed["text"] == "\n\n".join(
        [builtin_text("system"), "按我的顺序写。", builtin_text("scene_policy")])
    assert [part["key"] for part in composed["parts"]] == ["system", "writing", "scene_policy"]
    assert [part["source"] for part in composed["parts"]] == ["builtin", "skill", "builtin"]
    assert composed["parts"][1]["name"] == "我的写法"
    assert composed["parts"][1]["version"] == "2026-01-01T00:00:00Z"


def test_compose_skips_empty_segments(skills_file):
    """正文留空的替换技能等于去掉这一段：拼出来不留空行，parts 里也不出现它。"""
    write_rows(skills_file, row("空正文", replaces="writing", instructions="   "))
    composed = prompt_registry.compose(["system", "writing"])
    assert composed["text"] == builtin_text("system")
    assert [part["key"] for part in composed["parts"]] == ["system"]


def test_compose_rejects_unknown_key_and_bad_input():
    with pytest.raises(ValueError, match="不存在的提示词条目.*no_such"):
        prompt_registry.compose(["system", "no_such"])
    with pytest.raises(ValueError, match="提示词条目须为名称数组"):
        prompt_registry.compose("system")
    with pytest.raises(ValueError, match="不存在的提示词条目"):
        prompt_registry.resolve_prompt("no_such")


# ---------------------------------------------------------------- 来源与清单


def test_sources_describes_each_segment(skills_file):
    write_rows(skills_file, row("我的写法", replaces="writing", instructions="按我的顺序写。"))
    sources = prompt_registry.sources(["system", "writing"])
    assert sources["system"] == {"source": "builtin", "name": None,
                                 "version": str(DEFAULT_PROMPTS["system"]["version"])}
    assert sources["writing"] == {"source": "skill", "name": "我的写法",
                                  "version": "2026-01-01T00:00:00Z"}


def test_list_prompt_keys_shape_and_order():
    rows = prompt_registry.list_prompt_keys()
    assert [item["key"] for item in rows] == list(DEFAULT_PROMPTS)
    for item in rows:
        assert set(item) == {"key", "label", "description", "version", "replaced_by", "source"}
        assert item["label"] and item["description"]
        assert item["source"] == "builtin" and item["replaced_by"] is None


def test_template_and_snapshot_carry_prompt_sources(skills_file):
    write_rows(skills_file, row("我的写法", replaces="writing", instructions="按我的顺序写。"))
    definition = chat_templates.template("screenplay")
    assert definition["prompt_sources"]["writing"] == {
        "source": "skill", "name": "我的写法", "version": "2026-01-01T00:00:00Z"}
    assert definition["prompt_sources"]["system"]["source"] == "builtin"
    assert definition["prompt_versions"]["writing"] == "我的写法@2026-01-01T00:00:00Z"
    assert definition["prompt_versions"]["system"] == str(DEFAULT_PROMPTS["system"]["version"])

    frozen = chat_templates.snapshot("screenplay")
    assert frozen["prompt_sources"] == definition["prompt_sources"]
    assert "按我的顺序写。" in frozen["system_prompt"]
    # 冻结的是全文与来源：之后删掉技能，已冻结的快照不变。
    write_rows(skills_file)
    assert frozen["prompt_sources"]["writing"]["name"] == "我的写法"
    assert "按我的顺序写。" in frozen["system_prompt"]


# -------------------------------------------------------------------- 容错


def test_broken_skills_file_falls_back_and_reports_the_reason(skills_file):
    """技能文件坏掉时回落内置并带上原因：界面不能因为一个坏文件崩掉或整页空掉。"""
    skills_file.write_text("{ 这不是 JSON", encoding="utf-8")
    resolved = prompt_registry.resolve_prompt("writing")
    assert resolved["source"] == prompt_registry.BUILTIN
    assert resolved["text"] == builtin_text("writing")
    assert "不是合法 JSON" in resolved["problem"]

    composed = prompt_registry.compose(["system", "writing"])
    assert composed["text"] == "\n\n".join([builtin_text("system"), builtin_text("writing")])
    assert "不是合法 JSON" in composed["problem"]

    assert all(item["replaced_by"] is None for item in prompt_registry.list_prompt_keys())
    assert "不是合法 JSON" in prompt_registry.skill_problem()
    # 模板拼接照常可用，只是每一段都回落到内置。
    assert chat_templates.compose_system_prompt("screenplay").startswith(builtin_text("system")[:20])


def test_unreadable_encoding_falls_back_instead_of_raising(skills_file):
    """编码坏掉的文件（不是 UTF-8）同样只回落、不抛：界面读的是解析结果，不是文件本身。"""
    skills_file.write_bytes(b"\xff\xfe\x00 not utf-8")
    resolved = prompt_registry.resolve_prompt("system")
    assert resolved["source"] == prompt_registry.BUILTIN
    assert resolved["text"] == builtin_text("system")
    assert resolved["problem"] and prompt_registry.skill_problem()
