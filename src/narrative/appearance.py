"""应用外观偏好：浅色、深色或跟随系统，以及紧凑／岛屿两种主框构。

外观是应用级配置，不属于叙事数据，也不随工程包导出。它单独存成一个很小的
JSON 文件，界面启动时读一次、用户切换时整份重写。读取失败、文件缺失或内容
不是合法取值时回落到默认值：外观存坏了不该拦住界面启动。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MODES = ("system", "light", "dark")
DEFAULT_MODE = "system"

# 主框构：紧凑（默认，区域共边）与岛屿（区域浮成独立卡片，卡片之间留缝）。
LAYOUTS = ("compact", "islands")
DEFAULT_LAYOUT = "compact"


def default_path() -> Path:
    override = os.environ.get("NARRATIVE_APPEARANCE_FILE")
    if override:
        return Path(override)
    return Path.home() / ".narrative" / "appearance.json"


def _read(target: Path) -> dict:
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(payload: dict, target: Path) -> None:
    """原子写入（临时文件 + 替换），避免半份文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def load(path: str | Path | None = None) -> str:
    """读取已保存的外观模式；任何读不出来的情况都回落到跟随系统。"""
    mode = _read(Path(path) if path else default_path()).get("mode")
    return mode if mode in MODES else DEFAULT_MODE


def load_layout(path: str | Path | None = None) -> str:
    """读取已保存的主框构；读不出来或取值不认识时回落到紧凑布局。"""
    layout = _read(Path(path) if path else default_path()).get("layout")
    return layout if layout in LAYOUTS else DEFAULT_LAYOUT


def save(mode: str, path: str | Path | None = None, layout: str | None = None) -> None:
    """写入外观模式；同一份文件里的布局选择原样保留（除非显式传 layout）。"""
    if mode not in MODES:
        raise ValueError(f"未知外观模式：{mode}")
    target = Path(path) if path else default_path()
    payload = {"mode": mode}
    chosen = layout if layout is not None else _read(target).get("layout")
    if chosen in LAYOUTS:
        payload["layout"] = chosen
    _write(payload, target)


def save_layout(layout: str, path: str | Path | None = None) -> None:
    """写入主框构；同一份文件里的外观模式原样保留。"""
    if layout not in LAYOUTS:
        raise ValueError(f"未知布局：{layout}")
    target = Path(path) if path else default_path()
    payload = {}
    mode = _read(target).get("mode")
    if mode in MODES:
        payload["mode"] = mode
    payload["layout"] = layout
    _write(payload, target)


def is_dark(mode: str, system_dark: bool) -> bool:
    """把「模式 + 系统当前深浅色」解析成实际是否使用深色。"""
    if mode == "dark":
        return True
    if mode == "light":
        return False
    return system_dark
