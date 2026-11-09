"""Prune run artifacts. Dry-run by default; every deletion is logged with path, size and mtime.

Scope is deliberately narrow — only the categories below are inspected, the repository is
never walked as a whole:

``artifacts/{logs,probes,screenshots,extraction-consistency}``  run dirs, newest ``--keep-last`` kept
``out/``, ``.pytest_cache/``, ``htmlcov/``, ``.coverage``                  build/test caches
``__pycache__/`` and ``*.pyc`` under ``src/``, ``tests/``, ``scripts/``     byte-code
``TASKS-*.md`` at the repository root                                      member ledgers (TASKS.md excluded)
``%TEMP%/probe-*``                                                         only with ``--include-temp``

Hard protections are spelled out in ``PROTECTED_DIRS`` / ``PROTECTED_NAMES``: sources, tests,
docs (and ``docs/archive`` in particular), examples, scripts, virtualenv, git metadata,
``pyproject.toml`` and any ``.env*``/``secrets*`` file are never deleted, whatever flag is used.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "artifacts" / "logs"
SIZE_LIMIT = 2 * 1024 ** 3
DEFAULT_DAYS = 7
LOGS_KEEP_LAST = 20
CATEGORY_KEEP_LAST = 2

# 永不自动删除：命中即跳过，并在报告里列「受保护跳过」。
PROTECTED_DIRS = (
    "src", "tests", "docs", "examples", "scripts", ".venv", ".git", "docs/archive", ".runtime",
)
PROTECTED_NAMES = ("pyproject.toml", ".gitignore")
PROTECTED_PREFIXES = (".env", "secrets", "secret")

RUN_DIR_CATEGORIES = (
    ("artifacts/logs", LOGS_KEEP_LAST),
    ("artifacts/probes", CATEGORY_KEEP_LAST),
    ("artifacts/screenshots", CATEGORY_KEEP_LAST),
    ("artifacts/extraction-consistency", CATEGORY_KEEP_LAST),
)
CACHE_DIRS = ("out", ".pytest_cache", "htmlcov")
CACHE_FILES = (".coverage",)
BYTECODE_ROOTS = ("src", "tests", "scripts")
TEMP_PREFIX = "probe-"

EXIT_OK = 0
EXIT_NEEDS_FORCE = 2
EXIT_PROTECTED = 3


@dataclass(frozen=True)
class Entry:
    """One candidate for deletion."""

    path: Path
    size: int
    mtime: float
    category: str


@dataclass
class Plan:
    entries: list[Entry] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(entry.size for entry in self.entries)

    def subtotals(self) -> dict[str, tuple[int, int]]:
        totals: dict[str, tuple[int, int]] = {}
        for entry in self.entries:
            count, size = totals.get(entry.category, (0, 0))
            totals[entry.category] = (count + 1, size + entry.size)
        return totals


def protected_reason(path: Path, root: Path | None = None, *, ignore_dirs: bool = False) -> str | None:
    """Why ``path`` must not be deleted, or ``None`` when it is fair game.

    ``ignore_dirs`` 只给字节码清理用：``__pycache__``／``*.pyc`` 是派生文件，虽然长在
    ``src/``、``tests/``、``scripts/`` 里，重建成本为零；源码与文档本身仍受保护。
    """
    root = (root or ROOT).resolve()
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return "在仓库之外"
    parts = relative.parts
    if not parts:
        return "就是仓库根目录"
    joined = "/".join(parts)
    for guarded in (() if ignore_dirs else PROTECTED_DIRS):
        if joined == guarded or joined.startswith(guarded + "/"):
            return f"受保护目录 {guarded}"
    name = parts[-1]
    if name in PROTECTED_NAMES:
        return f"受保护文件 {name}"
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return "疑似密钥文件"
    return None


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for base, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
    return total


def dir_mtime(path: Path) -> float:
    """最近一次真实写入的时间：目录项自身的 mtime 会因列举、临时文件而变新。"""
    try:
        own = path.stat().st_mtime
    except OSError:
        return 0.0
    if not path.is_dir():
        return own
    newest = None
    for base, _, files in os.walk(path):
        for name in files:
            try:
                stamp = (Path(base) / name).stat().st_mtime
            except OSError:
                continue
            newest = stamp if newest is None else max(newest, stamp)
    return own if newest is None else newest


def select_run_dirs(entries: list[tuple[Path, float]], days: int, keep_last: int, *,
                    now: float | None = None) -> list[tuple[Path, float]]:
    """Keep the newest ``keep_last`` runs; older ones past ``days`` go away."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    newest_first = sorted(entries, key=lambda item: item[1], reverse=True)
    return [item for rank, item in enumerate(newest_first) if rank >= keep_last and item[1] < cutoff]


def _entry(path: Path, category: str, *, root: Path, plan: Plan, ignore_dirs: bool = False) -> None:
    reason = protected_reason(path, root, ignore_dirs=ignore_dirs)
    if reason:
        plan.skipped.append((path, reason))
        return
    try:
        stat = path.stat()
    except OSError:
        plan.skipped.append((path, "无法读取"))
        return
    plan.entries.append(Entry(path, dir_size(path), stat.st_mtime, category))


def _run_dir_category(root: Path, relative: str, days: int, keep_last: int, plan: Plan, *,
                      now: float) -> None:
    base = root / relative
    reason = protected_reason(base, root)
    if reason:
        plan.skipped.append((base, reason))
        return
    if not base.is_dir():
        return
    children = []
    for child in base.iterdir():
        try:
            children.append((child, dir_mtime(child)))
        except OSError:
            continue
    for path, _ in select_run_dirs(children, days, keep_last, now=now):
        _entry(path, relative, root=root, plan=plan)


def _cache_paths(root: Path, days: int, plan: Plan, *, now: float) -> None:
    cutoff = now - days * 86400
    for name in CACHE_DIRS:
        path = root / name
        reason = protected_reason(path, root)
        if reason:
            plan.skipped.append((path, reason))
            continue
        if not path.exists():
            continue
        if path.stat().st_mtime < cutoff:
            _entry(path, name, root=root, plan=plan)
    for name in CACHE_FILES:
        path = root / name
        if path.is_file() and path.stat().st_mtime < cutoff:
            _entry(path, name, root=root, plan=plan)


def _bytecode_paths(root: Path, days: int, plan: Plan, *, now: float) -> None:
    cutoff = now - days * 86400
    for name in BYTECODE_ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for current, dirs, files in os.walk(base):
            here = Path(current)
            if "__pycache__" in dirs:
                cache = here / "__pycache__"
                if dir_mtime(cache) < cutoff:
                    _entry(cache, "__pycache__", root=root, plan=plan, ignore_dirs=True)
                dirs.remove("__pycache__")
            for filename in files:
                if not filename.endswith(".pyc"):
                    continue
                compiled = here / filename
                try:
                    if compiled.stat().st_mtime < cutoff:
                        _entry(compiled, "*.pyc", root=root, plan=plan, ignore_dirs=True)
                except OSError:
                    continue


def _ledger_paths(root: Path, days: int, plan: Plan, *, now: float) -> None:
    cutoff = now - days * 86400
    for path in sorted(root.glob("TASKS-*.md")):
        if path.stat().st_mtime < cutoff:
            _entry(path, "TASKS-*.md", root=root, plan=plan)


def _temp_paths(plan: Plan, *, now: float, days: int, root: Path) -> None:
    base = Path(tempfile.gettempdir())
    if not base.is_dir():
        return
    cutoff = now - days * 86400
    for path in base.glob(TEMP_PREFIX + "*"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        if protected_reason(path, root):
            continue
        plan.entries.append(Entry(path, dir_size(path), stat.st_mtime, "%TEMP%/probe-*"))


def build_plan(root: Path | None = None, *, days: int = DEFAULT_DAYS, keep_last: int | None = None,
               include_temp: bool = False, now: float | None = None) -> Plan:
    root = (root or ROOT).resolve()
    now = time.time() if now is None else now
    plan = Plan()
    for relative, default_keep in RUN_DIR_CATEGORIES:
        limit = default_keep if keep_last is None else keep_last
        _run_dir_category(root, relative, days, limit, plan, now=now)
    _cache_paths(root, days, plan, now=now)
    _bytecode_paths(root, days, plan, now=now)
    _ledger_paths(root, days, plan, now=now)
    if include_temp:
        _temp_paths(plan, now=now, days=days, root=root)
    return plan


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def format_report(plan: Plan, *, mode: str, days: int) -> str:
    lines = [f"# {mode}（保留 {days} 天）", "",
             f"共 {len(plan.entries)} 项 / {human_size(plan.total_size)}"]
    for category, (count, size) in sorted(plan.subtotals().items()):
        lines.append(f"- {category}: {count} 项 / {human_size(size)}")
    if plan.skipped:
        lines += ["", f"受保护跳过 {len(plan.skipped)} 项："]
        lines += [f"  {path} — {reason}" for path, reason in plan.skipped[:50]]
    if plan.entries:
        lines += ["", "清单："]
        for entry in plan.entries:
            stamp = datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {human_size(entry.size):>9}  {stamp}  {entry.path}")
    return "\n".join(lines) + "\n"


def write_log(text: str, *, root: Path | None = None, now: datetime | None = None) -> Path:
    root = (root or ROOT).resolve()
    directory = root / "artifacts" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    path = directory / f"prune-{stamp}.log"
    path.write_text(text, encoding="utf-8")
    return path


def apply_plan(plan: Plan) -> list[Path]:
    removed = []
    for entry in plan.entries:
        try:
            if entry.path.is_dir() and not entry.path.is_symlink():
                shutil.rmtree(entry.path)
            else:
                entry.path.unlink()
        except OSError as error:
            plan.skipped.append((entry.path, f"删除失败：{error}"))
            continue
        removed.append(entry.path)
    return removed


def exit_code(plan: Plan, *, force: bool) -> int:
    if plan.total_size > SIZE_LIMIT and not force:
        return EXIT_NEEDS_FORCE
    return EXIT_PROTECTED if plan.skipped else EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清理运行产物（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真的删除；默认只出清单")
    parser.add_argument("--dry-run", action="store_true", help="只列清单（默认行为）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="保留天数，默认 7")
    parser.add_argument("--keep-last", type=int, default=None,
                        help=f"artifacts/logs 保留份数（默认 {LOGS_KEEP_LAST}），其它类别默认 {CATEGORY_KEEP_LAST}")
    parser.add_argument("--include-temp", action="store_true", help="纳入 %%TEMP%% 下的 probe-* 目录")
    parser.add_argument("--force", action="store_true", help=f"总量超过 {human_size(SIZE_LIMIT)} 时才允许清理")
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根目录（测试用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.days < 0:
        print("--days 不能为负", file=sys.stderr)
        return EXIT_NEEDS_FORCE
    plan = build_plan(args.root, days=args.days, keep_last=args.keep_last,
                      include_temp=args.include_temp)
    code = exit_code(plan, force=args.force)
    mode = "apply" if args.apply else "dry-run"
    report = format_report(plan, mode=mode, days=args.days)
    print(report)
    if code == EXIT_NEEDS_FORCE:
        print(f"总量 {human_size(plan.total_size)} 超过阈值 {human_size(SIZE_LIMIT)}，需加 --force", file=sys.stderr)
    elif args.apply:
        removed = apply_plan(plan)
        print(f"已删除 {len(removed)} 项 / {human_size(plan.total_size)}")
    log = write_log(report, root=args.root)
    print(f"日志：{log}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
