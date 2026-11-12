"""Check the desktop Python paths without shell-embedded Python source."""

import os
import sys
import sysconfig
from pathlib import Path

import narrative
from narrative.qt_preflight import check_qt_imports
from narrative.runtime_security import (
    ensure_medium_labels,
    format_label_report,
    integrity_label_status,
    integrity_label_summary,
    integrity_label_targets,
    require_medium_integrity,
)

root = Path(__file__).resolve().parents[1]


def print_integrity_labels() -> None:
    """常态可见：每次启动都打印解释器标签状态，谁说一句「有没有复发」都能自查。"""
    for line in integrity_label_summary(integrity_label_targets(root)):
        print(line)


def fix_integrity_labels() -> int:
    """把解释器树上的 Low 完整性标签改回 Medium（幂等；启动脚本与人工排查共用）。

    根因：代理会话在沙箱里做环境准备（uv sync / setup-environment / localize_python），
    它创建的解释器文件被打上 Low 标签，而 Windows 对带 Low 标签的 exe **一律以 Low
    启动**——父进程是 Medium 都救不回来。低完整性进程写不了 %USERPROFILE%\\.narrative，
    SQLite 把它报成「attempt to write a readonly database」。所以修的是标签，不是终端。
    """
    targets = integrity_label_targets(root)
    report = ensure_medium_labels(targets)
    for line in format_label_report(report):
        print(line)
    for line in integrity_label_status(targets):
        print(line)
    if report["failed"]:
        print("标签修复未完成：请以中等权限运行 scripts/fix_venv_integrity.ps1", file=sys.stderr)
        return 1
    return 0


# 只在需要修标签时进入：这条路径不碰 Qt，能在任何完整性级别下先跑。
if "--fix-integrity-labels" in sys.argv:
    raise SystemExit(fix_integrity_labels())

print_integrity_labels()

for name, path in (
    ("prefix", sys.prefix),
    ("base_prefix", sys.base_prefix),
    ("stdlib", sysconfig.get_path("stdlib")),
    ("application", narrative.__file__),
):
    if not Path(path).resolve().is_relative_to(root):
        raise RuntimeError(f"Python environment escapes the project: {name}={path!r}; project={str(root)!r}")

try:
    require_medium_integrity()
except RuntimeError as exc:
    # 低完整性只是可能写不了，不是必然：用真实写入探测决定能否启动。
    print(str(exc), file=sys.stderr)
    probe = root / f".runtime-write-probe-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        raise SystemExit(1) from None

check_qt_imports()
