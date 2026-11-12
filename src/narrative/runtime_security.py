"""Read (and repair) Windows integrity levels before starting the desktop.

低完整性有两个来源：①解释器文件带着 Low 完整性标签——Windows 对带 Low 标签的
exe **一律以 Low 启动**，父进程是不是 Medium 都一样；②从受限终端/沙箱启动时
父进程的低完整性令牌被继承。两者都会让进程写不了 ``%USERPROFILE%\\.narrative``，
SQLite 把这件事报成 "attempt to write a readonly database"。标签可以自动修
（``ensure_medium_labels``），令牌不行——只能显式提示或走逃生口。
"""

import ctypes
import logging
import os
import sys
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

MEDIUM_INTEGRITY = 0x2000

# 逃生口：受限终端里做 QA/自动化时才用。设了它，低完整性不再被拦，
# 但界面必须常驻一条降级提示（见 LOW_INTEGRITY_NOTICE）。
ALLOW_LOW_INTEGRITY_ENV = "NARRATIVE_ALLOW_LOW_INTEGRITY"

LOW_INTEGRITY_NOTICE = (
    "低完整性进程：改动不会写进用户目录（最近工程、模型设置保存不下来）"
)

# 真正的根因不是「谁启动的」，而是**解释器自己带着 Low 完整性标签**：
# Windows 对带 Low 标签的 exe 一律以 Low 启动，父进程是不是 Medium 都救不回来。
# 标签是低完整性进程（代理会话里的 uv sync / setup-environment / localize_python）
# 创建这些文件时打上的，所以修法是把解释器树重新设成 Medium，而不是去查终端。
# 递归只给 .venv：.runtime\python 带完整 CPython 文档，全量 /T 会让启动慢到不可接受，
# 那里只打目录与关键 exe（进程用的是 exe，不会用到成千上万个文档文件）。
INTEGRITY_TREES = (".venv", ".runtime/python")
INTEGRITY_EXES = (
    ".venv/Scripts/python.exe",
    ".venv/Scripts/pythonw.exe",
    ".runtime/python/python.exe",
    ".runtime/python/pythonw.exe",
)
LOW_LABEL_MARK = "low mandatory level"
_ICACLS_MARK = "mandatory label"


def integrity_label_targets(root) -> list[tuple[Path, bool]]:
    """要检查的 ``(路径, 是否递归)`` 清单：两棵解释器树 + 各自的 python/pythonw。"""
    root = Path(root)
    targets = [(root / tree, tree == ".venv") for tree in INTEGRITY_TREES]
    targets.extend((root / exe, False) for exe in INTEGRITY_EXES)
    return targets


def icacls_reports_low_label(output: str) -> bool:
    """纯判定：icacls 输出里是否带 Low 完整性标签。

    真实输出形如 ``Mandatory Label\\Low Mandatory Level:(OI)(CI)(NW)``；中文系统
    上标签名可能被本地化，所以只认「Mandatory Label」+「Low Mandatory Level」这对
    关键词，任一缺失都按「不是 Low」处理（宁可漏报，也不误改）。
    """
    for line in str(output).splitlines():
        lowered = line.lower()
        if _ICACLS_MARK in lowered and LOW_LABEL_MARK in lowered:
            return True
    return False


def integrity_label_command(path, *, recursive: bool = False, directory: bool = True) -> list[str]:
    """把这条路径（可选连同子树）的完整性标签设成 Medium；只动标签，不动权限。

    目录用继承标记 ``(OI)(CI)Medium``，**文件只能用裸 ``Medium``**：icacls 对文件
    不接受继承标记（会报错而标签不变），这就是「修了还是 Low」的来源。
    """
    label = "(OI)(CI)Medium" if directory else "Medium"
    argv = ["icacls", str(path), "/setintegritylevel", label]
    if recursive:
        argv.append("/T")
    return argv + ["/C", "/Q"]


def _icacls(argv: list[str]) -> str:
    import subprocess

    completed = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=600)
    return (completed.stdout or "") + (completed.stderr or "")


def _as_targets(targets) -> list[tuple[Path, bool]]:
    """接受 ``Path`` 或 ``(Path, recursive)`` 两种写法，测试与调用方都不用记细节。"""
    normalized = []
    for item in targets:
        if isinstance(item, tuple):
            normalized.append((Path(item[0]), bool(item[1])))
        else:
            normalized.append((Path(item), False))
    return normalized


def ensure_medium_labels(targets, *, run=_icacls) -> dict:
    """检查这些路径的完整性标签，带 Low 的就重新设成 Medium（启动自愈的主入口）。

    幂等、可失败：修不动也要如实返回（上游据此写日志/提示），绝不静默当成功。
    返回 ``{"checked","low","repaired","failed","skipped"}``；``failed`` 是
    ``(路径, 原因)`` 列表。
    """
    report: dict = {"checked": [], "low": [], "repaired": [], "failed": [], "skipped": []}
    normalized = _as_targets(targets)
    if sys.platform != "win32":
        report["skipped"] = [str(path) for path, _recursive in normalized]
        return report
    for path, recursive in normalized:
        text_path = str(path)
        if text_path in report["checked"]:
            continue
        if not path.exists():
            report["skipped"].append(text_path)
            continue
        report["checked"].append(text_path)
        try:
            before = run(["icacls", text_path])
        except Exception as exc:  # noqa: BLE001 - 诊断失败不该带崩启动
            report["failed"].append((text_path, f"icacls 读取失败：{exc}"))
            continue
        if not icacls_reports_low_label(before):
            continue
        report["low"].append(text_path)
        try:
            run(integrity_label_command(text_path, recursive=recursive, directory=path.is_dir()))
            after = run(["icacls", text_path])
        except Exception as exc:  # noqa: BLE001
            report["failed"].append((text_path, f"icacls 设置失败：{exc}"))
            continue
        if icacls_reports_low_label(after):
            report["failed"].append((text_path, "设置后仍然是 Low 标签"))
        else:
            report["repaired"].append(text_path)
    return report


def integrity_label_status(targets, *, run=_icacls) -> list[str]:
    """一行一条的标签状态（``level=Medium|Low|absent|unknown``），只读不改。

    常态可见：每次启动都能看到这四行，谁看一眼就知道有没有复发。
    """
    normalized = _as_targets(targets)
    lines: list[str] = []
    for path, _recursive in normalized:
        text_path = str(path)
        if not path.exists():
            lines.append(f"integrity_label path={text_path} level=absent")
            continue
        try:
            output = run(["icacls", text_path])
        except Exception as exc:  # noqa: BLE001
            lines.append(f"integrity_label path={text_path} level=unknown reason={exc}")
            continue
        lines.append(
            f"integrity_label path={text_path} level="
            + ("Low" if icacls_reports_low_label(output) else "Medium")
        )
    return lines


def integrity_label_summary(targets, *, run=_icacls) -> list[str]:
    """常态可见的那一行：解释器标签是 Medium / Low / absent，Low 的点名到路径。

    启动输出里只留这一行（逐条明细在 ``integrity_label_status`` 里，修复模式才打）。
    """
    lines = integrity_label_status(targets, run=run)
    low: list[str] = []
    unknown: list[str] = []
    absent = 0
    for line in lines:
        path = line.split("path=", 1)[-1].split(" level=", 1)[0]
        if line.endswith("level=Low"):
            low.append(path)
        elif line.endswith("level=absent"):
            absent += 1
        elif "level=unknown" in line:
            unknown.append(path)
    if low:
        return [f"integrity_label summary level=Low low={','.join(low)}"]
    if unknown:
        return [f"integrity_label summary level=unknown paths={','.join(unknown)}"]
    return [f"integrity_label summary level=Medium checked={len(lines) - absent} absent={absent}"]


def format_label_report(report: dict) -> list[str]:
    """把 ensure_medium_labels 的结果压成一行一条的日志（启动脚本原样写进 launch.log）。"""
    lines: list[str] = []
    failed = {path: reason for path, reason in report.get("failed", [])}
    for path in report.get("repaired", []):
        lines.append(f"integrity_label path={path} status=repaired_low_to_medium")
    for path in report.get("low", []):
        if path in failed:
            lines.append(f"integrity_label path={path} status=repair_failed reason={failed[path]}")
        elif path not in report.get("repaired", []):
            lines.append(f"integrity_label path={path} status=low_unresolved")
    for path in report.get("checked", []):
        if path not in report.get("low", []):
            lines.append(f"integrity_label path={path} status=medium_or_higher")
    for path in report.get("skipped", []):
        lines.append(f"integrity_label path={path} status=absent")
    return lines


def low_integrity_blocks(level: int | None) -> bool:
    """低于中完整性才算受限。取不到（None）时按「不受限」处理，绝不误拦。"""
    return level is not None and level < MEDIUM_INTEGRITY


def allow_low_integrity_requested(environ: Mapping[str, str] | None = None) -> bool:
    """逃生口是否被显式打开（环境变量形式，子进程会继承）。"""
    source = os.environ if environ is None else environ
    return source.get(ALLOW_LOW_INTEGRITY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def startup_refusal(level: int | None, *, allow: bool) -> str | None:
    """纯判定：这次启动该不该被拦下。返回拒绝理由，可以继续则返回 None。

    低完整性不是「再警告一次」的事：这个进程写不了 ``C:\\Users\\<user>\\.narrative``，
    最近工程、模型设置、会话全都会失败，而 pythonw 没有控制台，用户看不到任何提示。
    所以默认硬失败，只有显式的 ``-AllowLowIntegrity`` / 环境变量才放行。
    """
    if not low_integrity_blocks(level) or allow:
        return None
    return (
        f"当前进程是低完整性（完整性级别 0x{level:04x}），写不了用户目录"
        "（C:\\Users\\<用户名>\\.narrative）：最近工程、模型设置和会话都保存不下来。\n\n"
        "这不是应用自己降的完整性，也不是「权限没设对」。最常见的原因是解释器文件"
        "带着 Low 完整性标签：Windows 对带 Low 标签的 exe 一律以 Low 启动，"
        "从资源管理器双击也一样；标签是低完整性进程（沙箱/代理会话里的 uv sync、"
        "setup-environment、localize_python）创建这些文件时打上的。"
        f"start-desktop.ps1 会自动把 {', '.join(INTEGRITY_TREES)} 的标签改回 Medium，"
        "现场诊断用 icacls <exe> | findstr Mandatory 与 whoami /groups | findstr Mandatory。\n\n"
        "另一种原因是从受限终端/沙箱启动，父进程的低完整性令牌被继承下来。"
        "两种情况的处理都一样：关闭这个进程，在资源管理器里双击 start-desktop.cmd"
        "（或从开始菜单/桌面快捷方式启动）。\n"
        f"确实要在受限终端里调试，就加 -AllowLowIntegrity 或设 {ALLOW_LOW_INTEGRITY_ENV}=1；"
        "那样界面会一直挂着「改动不会写进用户目录」的提示。"
    )


def low_integrity_notice(level: int | None) -> str:
    """纯判定：界面常驻的降级提示；不是低完整性就返回空串。"""
    return LOW_INTEGRITY_NOTICE if low_integrity_blocks(level) else ""


def current_integrity_notice() -> str:
    """当前进程该挂的降级提示。读不到完整性时不拦也不提示，只记一行日志。"""
    level = current_integrity_level()
    if level is None:
        logger.info("进程完整性级别读取不到，按正常启动处理（不拦截、不提示）")
        return ""
    return low_integrity_notice(level)


def current_integrity_level() -> int | None:
    """Return the process token's mandatory-label RID; other platforms skip it."""
    if sys.platform != "win32":
        return None
    kernel, security = _token_functions()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _label_rid(token, kernel, security)


def _token_functions():
    """Configure and return (kernel32, advapi32) with the signatures we use."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             wintypes.LPVOID, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.GetSidSubAuthorityCount.argtypes = [wintypes.LPVOID]
    security.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    security.GetSidSubAuthority.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    security.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    return kernel, security


def _label_rid(token, kernel, security) -> int:
    try:
        needed = wintypes.DWORD()
        security.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not security.GetTokenInformation(token, 25, buffer, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_MANDATORY_LABEL starts with SID_AND_ATTRIBUTES, whose first
        # field is the SID pointer. The label RID is its last sub-authority.
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        count = security.GetSidSubAuthorityCount(sid)[0]
        return int(security.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        kernel.CloseHandle(token)


def process_integrity_level(pid: int) -> int | None:
    """另一个进程的完整性级别（只读，尽力而为）。查不到就返回 None。

    启动日志靠它回答「是谁把我生成了低完整性」：父进程是 Medium 还是 Low，
    一眼就能看出是不是令牌继承。
    """
    if sys.platform != "win32":
        return None
    kernel, security = _token_functions()
    # PROCESS_QUERY_LIMITED_INFORMATION：对同用户的其他进程够用，也不需要调试权限。
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not security.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            return None
        try:
            return _label_rid(token, kernel, security)
        except OSError:  # 尽力而为：查不到就当查不到
            return None
    finally:
        # token 句柄由 _label_rid 关；这里只关进程句柄。
        kernel.CloseHandle(handle)


def require_medium_integrity() -> None:
    level = current_integrity_level()
    if level is not None and level < MEDIUM_INTEGRITY:
        raise RuntimeError(
            "本地 Python 运行时当前处于 Low（低完整性）进程：它继承了父进程的令牌，"
            "写不了普通用户目录和配置库（%USERPROFILE%\\.narrative）。"
            "这不是标签损坏，也不是应用自己降的——从受限终端、沙箱或代理里启动就会这样。"
            "请关闭后从桌面/资源管理器双击 start-desktop.cmd 重新启动；"
            "没有中完整性（Medium）可用时，start-desktop.ps1 -AllowLowIntegrity"
            f"（或设 {ALLOW_LOW_INTEGRITY_ENV}=1）可以显式放行，"
            "但界面会一直提示「改动不会写进用户目录」。"
        )
