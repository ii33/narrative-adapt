"""应用外观偏好：浅色、深色或跟随系统。

外观是应用级配置，不属于叙事数据，也不随工程包导出。它单独存成一个很小的
JSON 文件，界面启动时读一次、用户切换时整份重写。读取失败、文件缺失或内容
不是三种模式之一时回落到「跟随系统」：外观存坏了不该拦住界面启动。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("system", "light", "dark")
DEFAULT_MODE = "system"


def default_path() -> Path:
    override = os.environ.get("NARRATIVE_APPEARANCE_FILE")
    if override:
        return Path(override)
    return Path.home() / ".narrative" / "appearance.json"


def load(path: str | Path | None = None) -> str:
    """读取已保存的外观模式；任何读不出来的情况都回落到跟随系统。"""
    target = Path(path) if path else default_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_MODE
    mode = raw.get("mode") if isinstance(raw, dict) else None
    return mode if mode in MODES else DEFAULT_MODE


def save(mode: str, path: str | Path | None = None) -> None:
    """原子写入外观模式（临时文件 + 替换），避免半份文件。"""
    if mode not in MODES:
        raise ValueError(f"未知外观模式：{mode}")
    target = Path(path) if path else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({"mode": mode}, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def is_dark(mode: str, system_dark: bool) -> bool:
    """把「模式 + 系统当前深浅色」解析成实际是否使用深色。"""
    if mode == "dark":
        return True
    if mode == "light":
        return False
    return system_dark
