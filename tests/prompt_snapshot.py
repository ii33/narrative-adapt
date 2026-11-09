"""Prompt snapshot helpers shared by the guard test and the update script.

The snapshot records ``key -> {version, digest}`` for every ``DEFAULT_PROMPTS`` entry.
It does not judge wording: it only enforces the discipline "text changed ⇒ version
changed", so a silent edit of a prompt text cannot ship under an old version.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "prompt_snapshot.json"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def current_entries() -> dict[str, dict[str, str]]:
    from narrative.prompt_defaults import DEFAULT_PROMPTS

    return {
        key: {"version": str(entry.get("version", "1")), "digest": digest(entry["text"])}
        for key, entry in sorted(DEFAULT_PROMPTS.items())
    }


def load_snapshot(path: Path | None = None) -> dict:
    target = path or SNAPSHOT
    return json.loads(target.read_text(encoding="utf-8"))


def dump_snapshot(payload: dict, path: Path | None = None) -> Path:
    target = path or SNAPSHOT
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target


def compare(current: dict[str, dict[str, str]], snapshot: dict) -> list[dict[str, str]]:
    """Each entry: ``{kind, key, message}``; empty list means the snapshot matches."""
    recorded = {key: value for key, value in (snapshot.get("prompts") or {}).items()}
    allowed = set(snapshot.get("allow_version_only") or [])
    problems: list[dict[str, str]] = []
    for key in sorted(set(recorded) | set(current)):
        old, new = recorded.get(key), current.get(key)
        if old is None:
            problems.append({"kind": "added", "key": key,
                             "message": f"提示词 `{key}` 还没进快照（当前 v{new['version']}），"
                                        f"确认后运行 python scripts/update_prompt_snapshot.py --write"})
            continue
        if new is None:
            problems.append({"kind": "removed", "key": key,
                             "message": f"提示词 `{key}` 已从 DEFAULT_PROMPTS 删除，但快照里还有"
                                        f"（v{old['version']}）；确认后更新快照"})
            continue
        if old["version"] == new["version"] and old["digest"] != new["digest"]:
            problems.append({"kind": "text_without_version", "key": key,
                             "message": f"提示词 `{key}` 文本已改但版本仍是 v{new['version']}，"
                                        f"请先升版本再更新快照"})
        elif old["version"] != new["version"] and old["digest"] == new["digest"]:
            if key in allowed:
                continue
            problems.append({"kind": "version_without_text", "key": key,
                             "message": f"提示词 `{key}` 版本由 v{old['version']} 变为 v{new['version']} "
                                        f"但文本没变；无意义升版需在快照 allow_version_only 里登记"})
        elif old["version"] != new["version"] and old["digest"] != new["digest"]:
            problems.append({"kind": "outdated", "key": key,
                             "message": f"提示词 `{key}` 已是 v{new['version']}"
                                        f"（快照记的是 v{old['version']}），确认后更新快照"})
    return problems


def summarise(old: dict[str, dict[str, str]], new: dict[str, dict[str, str]]) -> list[str]:
    """Human-readable diff lines for the update script."""
    lines = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        if before is None:
            lines.append(f"+ {key}  新增 v{after['version']} {after['digest'][:8]}")
        elif after is None:
            lines.append(f"- {key}  删除（原 v{before['version']} {before['digest'][:8]}）")
        elif before["version"] != after["version"]:
            lines.append(f"~ {key}  v{before['version']} → v{after['version']}"
                         f"  {before['digest'][:8]} → {after['digest'][:8]}")
        else:
            lines.append(f"~ {key}  文本变了但版本仍是 v{after['version']}"
                         f"  {before['digest'][:8]} → {after['digest'][:8]}")
    return lines
