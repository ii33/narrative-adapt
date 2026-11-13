"""Which files a project entry really owns — and therefore what "delete" deletes.

The entry descriptor (``.nproj``) is the only authority on where a project keeps
its database, material, extraction cache and chats. Folder naming carries no
meaning: ``<stem>_data`` / ``_extraction`` / ``_chats`` are *defaults* a new
project starts with, not a contract, and a user is free to rename the folder and
fix the descriptor. Deletion therefore plans from the descriptor and deletes
what the descriptor names.

Only three things are genuinely dangerous, and each one is kept with a reason
the user can read:

* another project entry references (or overlaps) the directory — it is not ours
  alone any more;
* the directory is not inside the entry file's own folder — never reach outside
  the folder the user pointed at;
* the directory *is* that folder (or contains the entry file itself) — deleting
  it would take the entry, and everything beside it, with it.

Anything else is deleted. "It looks unusual, so keep it" is exactly the kind of
defensive silent retention that leaves the user's data on disk after they asked
for it to be gone.
"""
from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import project_package

REASON_ENTRY_FOLDER = "这就是工程入口所在的目录，删掉会连入口文件和其他文件一起删"
REASON_CONTAINS_ENTRY = "该目录里就放着工程入口文件，删掉会连入口一起删"
REASON_OUTSIDE = "该目录不在工程入口所在的目录内，不删除入口目录之外的内容"
REASON_SHARED_DIRS = "另一个工程入口也引用这里"
REASON_SHARED_ENTRY = "另一个工程入口就放在这个目录里"


@dataclass(frozen=True)
class RemovalPlan:
    """What an explicit delete will remove, and what it must refuse to touch."""

    entry: Path
    directories: list[Path] = field(default_factory=list)
    retained: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def retained_data(self) -> bool:
        return bool(self.retained)

    def retained_paths(self) -> list[dict[str, str]]:
        """Payload shape for QML: every kept item carries its human reason."""
        return [{"path": str(path), "reason": reason} for path, reason in self.retained]


@dataclass(frozen=True)
class RemovalOutcome:
    removed: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _resolved_directories(found: project_package.Layout) -> list[Path]:
    """Descriptor-named folders in a stable order, without exact duplicates."""
    candidates: list[Path] = []
    for value in (found.root, found.extraction, found.chats, found.material):
        resolved = value.resolve()
        if resolved not in candidates:
            candidates.append(resolved)
    return candidates


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _referenced(referenced: project_package.Layout) -> list[Path]:
    return [referenced.root.resolve(), referenced.db.resolve(),
            referenced.material.resolve(), referenced.extraction.resolve(),
            referenced.chats.resolve()]


def _other_usage(directory: Path, entries: list[Path]) -> str:
    for other in entries:
        if other in directory.parents or other == directory:
            return f"{REASON_SHARED_ENTRY}：{other.name}"
        try:
            referenced = project_package.layout(other)
        except (OSError, ValueError):
            continue
        if any(_overlaps(directory, path) for path in _referenced(referenced)):
            return f"{REASON_SHARED_DIRS}：{other.name}"
    return ""


def _retain_reason(directory: Path, target: Path, parent: Path,
                   others: list[Path]) -> str:
    """The only reasons to keep a folder the descriptor says is ours."""
    if directory.parent == directory:
        return "这是一个磁盘根目录，从不整盘删除"
    if directory == parent:
        return REASON_ENTRY_FOLDER
    if parent not in directory.parents:
        return REASON_OUTSIDE
    if target in directory.parents:
        return REASON_CONTAINS_ENTRY
    return _other_usage(directory, others)


def plan_removal(entry: str | Path, *, other_entries: Iterable[str | Path] = ()) -> RemovalPlan:
    """Plan the deletion of ``entry`` and the data it owns. Touches nothing.

    ``other_entries`` are the other project entries that could still reference
    the same folders (siblings plus remembered ones); unresolvable ones are
    ignored, since they cannot be using anything.
    """
    target = Path(entry).resolve()
    if target.suffix.lower() != project_package.PACKAGE_SUFFIX or not target.is_file():
        raise ValueError("这不是有效的 .nproj 工程入口文件，没有删除")
    found = project_package.layout(target)
    parent = target.parent
    others: list[Path] = []
    for candidate in other_entries:
        try:
            resolved = Path(candidate).resolve()
        except (OSError, ValueError):
            continue
        if resolved != target and resolved.is_file() and resolved not in others:
            others.append(resolved)

    deletable: list[Path] = []
    retained: list[tuple[Path, str]] = []
    for directory in _resolved_directories(found):
        if not directory.exists():
            continue
        reason = _retain_reason(directory, target, parent, others)
        if reason:
            retained.append((directory, reason))
        else:
            deletable.append(directory)
    # A folder nested inside another *deletable* folder goes with it; one nested
    # in a kept folder is still ours to delete (e.g. ``material`` inside the
    # entry's own folder). Nested folders of kept folders are therefore kept in
    # the plan rather than being absorbed by the outer one.
    directories = [path for path in deletable
                   if not any(other != path and other in path.parents for other in deletable)]
    # The same the other way round for kept ones: a kept folder already keeps
    # everything inside it, so naming its children again is noise.
    kept = [item for item in retained
            if not any(other != item[0] and other in item[0].parents
                       for other, _reason in retained)]
    return RemovalPlan(entry=target, directories=directories, retained=kept)


def apply_plan(plan: RemovalPlan) -> RemovalOutcome:
    """Delete the planned folders, then unlink the entry — always last.

    A folder that cannot be removed is reported, not swallowed, and does not
    stop the other folders or the entry: the user asked for the project to be
    gone, and an entry left behind pointing at half-deleted data is worse.
    """
    outcome = RemovalOutcome()
    for directory in plan.directories:
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            outcome.errors.append(f"{directory}：{exc}")
        else:
            outcome.removed.append(directory)
    try:
        plan.entry.unlink()
    except OSError as exc:
        outcome.errors.append(f"{plan.entry}：{exc}")
    else:
        outcome.removed.append(plan.entry)
    return outcome
