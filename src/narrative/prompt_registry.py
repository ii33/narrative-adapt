"""提示词位解析：内置默认（`prompt_defaults`）+ 用户技能的替换（`skills.replaces`）。

为什么要有这一层：

- 内置提示词随程序发布、只读。用户想改一段措辞时，过去只能改代码；技能现在可以
  声明 `replaces: "<键>"`，用它的 `instructions` 顶替那一段。
- 替换只发生在**解析时**：`DEFAULT_PROMPTS` 的文本一个字符都不改，所以提示词快照
  （`tests/prompt_snapshot.json`）继续只核对内置文本——用户改措辞不该触发升版，
  也不该让内置提示词跟着漂移。
- 同一条 `replaces` 允许多条技能存在（保存不拒绝：用户可以先备几条比较），解析时
  按 `updated_at` **最近更新优先**，并把被忽略的名单回报出来。静默丢弃会让人以为
  覆盖没生效，反而更难查。
- 读技能必须容错：技能文件坏掉时回落内置并记下原因。这一层会被界面（模板列表、
  会话快照）调用，一个坏文件不能让界面崩。

返回的 `source` 只有两个值：`"builtin"`（内置那段）与 `"skill"`（被某条技能顶替）。
技能被停用或删除后自动回落内置——不需要清理任何缓存，因为这里每次都按文件现算。
"""

from __future__ import annotations

from pathlib import Path

from . import skills
from .prompt_defaults import DEFAULT_PROMPTS

BUILTIN = "builtin"
SKILL = "skill"


def _entry(key: str) -> dict:
    """取一条内置提示词；未知键报错并列出可选值（技能拼错键时也走这里）。"""
    entry = DEFAULT_PROMPTS.get(key)
    if entry is None:
        raise ValueError("不存在的提示词条目：" + str(key)
                         + "；可选的提示词条目：" + "、".join(DEFAULT_PROMPTS))
    return entry


def _replacements(path: str | Path | None = None) -> tuple[dict[str, list[skills.Skill]], str | None]:
    """`replaces 键 → 启用的技能列表（最近更新在前）`；第二个返回值是读技能失败的原因。"""
    try:
        custom = skills.load_custom_skills(path)
    except (skills.SkillError, OSError, UnicodeDecodeError) as exc:
        # 技能是附加能力：文件坏了（不是 JSON、编码不对、读不动）就回落内置，
        # 把原因带回去让界面能显示、用户能修。
        return {}, f"技能文件读不出来，已回落到内置提示词：{exc}"
    index: dict[str, list[skills.Skill]] = {}
    for skill in custom:
        if not skill.enabled or not skill.replaces:
            continue
        index.setdefault(skill.replaces, []).append(skill)
    for rows in index.values():
        # 时间戳相同时再按名字排：解析结果必须稳定，否则界面上一会儿显示 A、
        # 一会儿显示 B，谁也说不清现在生效的是哪一条。
        rows.sort(key=lambda skill: (skill.updated_at or "", skill.name), reverse=True)
    return index, None


def _resolved(key: str, index: dict[str, list[skills.Skill]], problem: str | None) -> dict:
    """单个键的解析结果。文本为空的技能等于「把这段提示词去掉」（compose 会跳过空段）。"""
    entry = _entry(key)
    rows = index.get(key) or []
    chosen = rows[0] if rows else None
    text = (chosen.instructions if chosen is not None else entry.get("text", "")) or ""
    result = {
        "key": key,
        "text": text.strip(),
        "version": (chosen.updated_at or "") if chosen is not None else str(entry.get("version", "1")),
        "source": SKILL if chosen is not None else BUILTIN,
        "overridden_by": chosen.name if chosen is not None else None,
        "name": chosen.name if chosen is not None else None,
    }
    ignored = [skill.name for skill in rows[1:]]
    if ignored:
        # 被忽略的同键技能要报出来：用户可能以为更新的是另一条。
        result["ignored"] = ignored
    if problem:
        result["problem"] = problem
    return result


def resolve_prompt(key: str, *, path: str | Path | None = None) -> dict:
    """一个提示词位当前生效的文本与来源。

    返回 `{key, text, version, source, overridden_by, name}`；同一键还有别的技能时
    多一个 `ignored`（被忽略的技能名），技能文件读不出来时多一个 `problem`。
    `version`：内置是 `DEFAULT_PROMPTS` 里的版本号，技能是那条技能的 `updated_at`。
    """
    index, problem = _replacements(path)
    return _resolved(str(key), index, problem)


def compose(keys, *, path: str | Path | None = None) -> dict:
    """按顺序拼接若干提示词位，返回全文与逐段来源。

    `parts` 只记来源（key/source/name/version），不重复正文——正文已经在 `text` 里。
    空文本的段直接跳过（技能正文留空等于去掉这一段，而不是留下一个空行）。
    """
    if isinstance(keys, str) or not isinstance(keys, (list, tuple)):
        raise ValueError("提示词条目须为名称数组，例如 ['system', 'writing']")
    index, problem = _replacements(path)
    resolved = [_resolved(str(key), index, problem) for key in keys]
    parts = [
        {"key": item["key"], "source": item["source"],
         "name": item["name"], "version": item["version"]}
        for item in resolved if item["text"]
    ]
    result = {
        "text": "\n\n".join(item["text"] for item in resolved if item["text"]),
        "parts": parts,
    }
    if problem:
        result["problem"] = problem
    return result


def sources(keys, *, path: str | Path | None = None) -> dict[str, dict]:
    """每段提示词的来源与版本：`{key: {source, name, version}}`（快照 `prompt_sources` 用）。"""
    index, problem = _replacements(path)
    out: dict[str, dict] = {}
    for key in keys:
        item = _resolved(str(key), index, problem)
        out[item["key"]] = {"source": item["source"], "name": item["name"], "version": item["version"]}
    return out


def list_prompt_keys(path: str | Path | None = None) -> list[dict]:
    """全部提示词位及其当前覆盖关系，给界面与 `list_prompt_keys` 工具用。

    顺序沿用 `DEFAULT_PROMPTS` 的声明顺序（界面上的排列顺序与提示词文件的顺序一致）。
    技能文件坏掉时每一行的 `replaced_by` 都是 None（即内置生效），原因用
    `skill_problem()` 取，不在这里抛错——列表页不能因为一个坏文件整页空掉。
    """
    index, _problem = _replacements(path)
    rows = []
    for key, entry in DEFAULT_PROMPTS.items():
        candidates = index.get(key) or []
        chosen = candidates[0] if candidates else None
        rows.append({
            "key": key,
            "label": entry.get("label") or key,
            "description": entry.get("description") or "",
            "version": (chosen.updated_at or "") if chosen is not None else str(entry.get("version", "1")),
            "replaced_by": chosen.name if chosen is not None else None,
            "source": SKILL if chosen is not None else BUILTIN,
        })
    return rows


def skill_problem(path: str | Path | None = None) -> str | None:
    """技能文件当前读不出来的原因；能读就是 None。界面据此显示一行提示。"""
    return _replacements(path)[1]


__all__ = [
    "BUILTIN",
    "SKILL",
    "compose",
    "list_prompt_keys",
    "resolve_prompt",
    "skill_problem",
    "sources",
]
