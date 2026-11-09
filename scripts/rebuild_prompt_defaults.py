"""从 HEAD 的原始条目 + artifacts/prompt-drafts 的草稿，整表重建 prompt_defaults.py。

增量回写曾被切错边界，这里改为确定性重建：HEAD 提供条目顺序与未改动的条目，
草稿提供改写后的文本。system / extraction 无草稿文件，文本取 _recovered.json。

用法（在 narrative-adapt 目录下）：
  .venv/Scripts/python.exe scripts/rebuild_prompt_defaults.py
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "narrative" / "prompt_defaults.py"
DRAFTS = ROOT / "artifacts" / "prompt-drafts"
RECOVERED = DRAFTS / "_recovered.json"
RECOVERED_KEYS = ("system", "extraction")
# 本轮结构合并：三条写作快捷提示合成 writing_shortcut，批量写作并入 writing。
REMOVED = ("batch_writing", "scene_table_shortcut", "unit_writing_shortcut", "batch_writing_shortcut")
NEW_AFTER = {"writing_shortcut": "writing"}
MARKER = "DEFAULT_PROMPTS = {"


def head_source() -> str:
    return subprocess.run(
        ["git", "show", "HEAD:src/narrative/prompt_defaults.py"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8")


def parse_draft(path: Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8")
    header, _, rest = text.partition("\n---\n")
    meta: dict[str, str] = {}
    key = ""
    for line in header.splitlines():
        line = line.strip()
        if line.startswith("# "):
            key = line[2:].strip()
        elif ":" in line:
            name, _, value = line.partition(":")
            meta[name.strip()] = value.strip()
    for field in ("version", "label", "description"):
        assert meta.get(field), f"{path} 缺 {field}"
    body = rest.split("## 改动说明")[0].strip("\n")
    assert body.strip(), f"{path} 正文为空"
    return key, {"label": meta["label"], "description": meta["description"],
                 "version": meta["version"], "text": body}


def main() -> int:
    source = head_source()
    namespace = {"__name__": "prompt_head"}
    exec(compile(source, "prompt_head", "exec"), namespace)  # noqa: S102 - 解析 HEAD 的常量
    base = namespace["DEFAULT_PROMPTS"]

    items = {}
    for path in sorted(DRAFTS.glob("*.md")):
        key, data = parse_draft(path)
        assert key in base or key in NEW_AFTER, f"草稿 {path} 的键 {key} 既不在 HEAD 条目里也不是新增键"
        items[key] = data
    recovered = json.loads(RECOVERED.read_text(encoding="utf-8"))["current"]
    for key in RECOVERED_KEYS:
        # 草稿优先：_recovered 只是没有草稿时的快照兜底。
        if key in recovered and key not in items:
            record = recovered[key]
            items[key] = {"label": record["label"], "description": record["desc"],
                          "version": record["version"], "text": record["text"]}

    order: list[str] = []
    for key in base:
        if key in REMOVED:
            continue
        order.append(key)
        for new_key, after in NEW_AFTER.items():
            if after == key and new_key not in base:
                order.append(new_key)
    lines = [MARKER]
    for key in order:
        item = items.get(key) or base.get(key)
        assert item, f"{key} 既没有草稿也没有 HEAD 文本"
        label = str(item["label"]).replace('"', '\\"')
        desc = str(item["description"]).replace('"', '\\"')
        lines.append(
            f'    "{key}": _entry("{label}", "{desc}", """{item["text"]}""", version="{item["version"]}"),'
        )
    lines.append("}")
    rebuilt = source[: source.index(MARKER)] + "\n".join(lines) + "\n"
    if "\r\n" in source:
        rebuilt = rebuilt.replace("\r\n", "\n").replace("\n", "\r\n")
    TARGET.write_text(rebuilt, encoding="utf-8")
    print(f"重建 {len(base)} 条，其中 {len(items)} 条来自草稿/快照")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
