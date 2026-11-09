"""Update tests/prompt_snapshot.json after a human confirms the prompt change.

Prints a diff summary (key, old → new version, digest prefix) and writes only with
``--write``; nothing is updated silently.

    python scripts/update_prompt_snapshot.py          # 预演，只打印
    python scripts/update_prompt_snapshot.py --write   # 人工确认后写入
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from prompt_snapshot import SNAPSHOT, current_entries, dump_snapshot, load_snapshot, summarise  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="更新提示词版本快照")
    parser.add_argument("--write", action="store_true", help="确认后写入 tests/prompt_snapshot.json")
    args = parser.parse_args(argv)

    previous = load_snapshot() if SNAPSHOT.exists() else {"prompts": {}, "allow_version_only": []}
    prompts = current_entries()
    lines = summarise(previous.get("prompts") or {}, prompts)
    if not lines:
        print("快照已是最新，无需改动。")
        return 0
    print("将要变更：")
    for line in lines:
        print("  " + line)
    stale = [line for line in lines if "文本变了但版本仍是" in line]
    if stale:
        print("\n注意：以下条目改了文本却没升 version，按约定应先升 version 再更新快照：")
        for line in stale:
            print("  " + line)
    if not args.write:
        print("\n这是预演，没有写入；确认后加 --write。")
        return 0
    payload = {"prompts": prompts,
               "allow_version_only": sorted(set(previous.get("allow_version_only") or []))}
    target = dump_snapshot(payload)
    print(f"\n已写入 {target.relative_to(ROOT)}（{len(prompts)} 条）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
