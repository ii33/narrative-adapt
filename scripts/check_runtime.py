"""Check the desktop Python paths without shell-embedded Python source."""

import os
import sys
import sysconfig
from pathlib import Path

import narrative
from narrative.qt_preflight import check_qt_imports
from narrative.runtime_security import require_medium_integrity

root = Path(__file__).resolve().parents[1]
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
