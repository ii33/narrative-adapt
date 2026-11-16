"""任务身份：工程内序数 ``task-12``，不带时间戳和哈希。

任务目录名以前是 ``20261115T120634187839Z-3c5ed8d8``。那是给人看也记不住的代号，
一旦进模型（``read_task_write_summary``、``read_usage_statistics``、错误消息）就只是
噪音。现在按工程内创建顺序发号，计数落在 ``extraction/assistant/.task_ids``；
历史任务目录保持原样，只在读取时按出现顺序补映射，不改写、不迁移、不改名。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .project_package import layout
from .readable_ids import looks_generated, next_file_id

_COUNTER = ".task_ids"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _is_readable(identifier: str) -> bool:
    return bool(re.fullmatch(r"task-[1-9][0-9]*", identifier))


def _root(project) -> Path:
    return Path(layout(project).extraction) / "assistant"


def _index_path(project) -> Path:
    return _root(project) / _COUNTER


def _lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    legacy = data.get("legacy")
    legacy = legacy if isinstance(legacy, dict) else {}
    next_number = data.get("next")
    return {
        "next": next_number if type(next_number) is int and next_number > 0 else 1,
        "legacy": {stem: readable for stem, readable in legacy.items()
                   if isinstance(stem, str) and isinstance(readable, str) and _is_readable(readable)},
    }


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".task-ids-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _stamp(directory: Path) -> float:
    try:
        return (directory / "task.json").stat().st_mtime
    except OSError:
        try:
            return directory.stat().st_mtime
        except OSError:
            return time.time()


def _ensure_index(project) -> dict:
    """读编号索引；旧工程的 hash 任务目录按创建顺序补成 ``task-N``。"""
    path = _index_path(project)
    with _lock(path):
        index = _read(path)
        tasks = path.parent / "tasks"
        if not tasks.is_dir():
            return index
        missing = sorted((stem for stem in (item.name for item in tasks.iterdir() if item.is_dir())
                          if looks_generated(stem) and stem not in index["legacy"]),
                         key=lambda stem: (_stamp(tasks / stem), stem))
        if not missing:
            return index
        for stem in missing:
            number = index["next"]
            while (tasks / f"task-{number}").is_dir() or f"task-{number}" in index["legacy"].values():
                number += 1
            index["legacy"][stem] = f"task-{number}"
            index["next"] = number + 1
        _write(path, index)
        return index


def task_file_stem(project, task_id: str) -> str:
    """任务编号 → 审计目录名（可读序数与历史 hash 编号都能认）。"""
    identifier = str(task_id or "").strip()
    index = _ensure_index(project)
    for stem, readable in index["legacy"].items():
        if readable == identifier:
            return stem
    return identifier


def readable_task_id(project, task_id: str) -> str:
    """任何形态的任务编号 → 可读编号；进模型的字段只用这一个。"""
    identifier = str(task_id or "").strip()
    if not identifier or _is_readable(identifier):
        return identifier
    for stem, readable in _ensure_index(project)["legacy"].items():
        if stem == identifier:
            return readable
    return identifier


def next_task_id(project) -> str:
    """Allocate the next readable task ordinal; the audit directory is created by the caller."""
    path = _index_path(project)
    with _lock(path):
        index = _ensure_index(project)
        tasks = path.parent / "tasks"
        taken = set(index["legacy"].values()) | {item.name for item in tasks.glob("task-*") if item.is_dir()}
        identifier = next_file_id(index, "task-", taken)
        _write(path, index)
        return identifier


def task_reference(task_id: str) -> str:
    """A message fragment naming one task without exposing a hash."""
    identifier = str(task_id or "").strip()
    if not identifier:
        return "任务记录"
    if _is_readable(identifier):
        return f"任务 {identifier}"
    return "任务记录"
