"""原生桌面启动器：启动自检与拦截都在这里，批处理入口只剩三件哑事。

启动链：``start-desktop.cmd`` -> ``.venv\\Scripts\\python.exe -I -m narrative.launcher``
-> ``.venv\\Scripts\\pythonw.exe -I -m narrative.native_app``。

为什么不再靠 PowerShell：Store 版 ``pwsh`` 的别名不在 PATH 时，``.cmd`` 只能靠猜几个
绝对路径，猜不中就报「未找到」；更要紧的是启动逻辑（运行时自检、解释器标签自愈、
低完整性拦截）写在 ``.ps1`` 里就永远进不了默认 pytest 门禁。逻辑搬进 Python 后既是
唯一真源，判定与组装也都能被单测直接断言——批处理只保留「找 python、透传参数、
按退出码决定是否 ``pause``」。

退出码（``.cmd`` 与外部脚本据此判断这次启动到底成没成）：

===== ==========================================================================
0     启动成功（``-QaExitAfterSeconds`` 时应用自己也退 0）；``-PrepareOnly`` 自检通过
2     参数不认识／不合法
3     虚拟环境或本地运行时不全（缺文件、版本不匹配、重定向器缺失）
4     低完整性硬拦（延续旧脚本的 4，见 ``runtime_security.startup_refusal``）
5     ``.runtime/python`` 准备／修复失败
6     启动自检（``scripts/check_runtime.py``）失败
7     定时 QA 到点没退出
8    pythonw 起不来
===== ==========================================================================
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from . import runtime_security

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VENV_INCOMPLETE = 3
EXIT_LOW_INTEGRITY_BLOCKED = 4
EXIT_RUNTIME_PREPARE_FAILED = 5
EXIT_STARTUP_CHECKS_FAILED = 6
EXIT_QA_TIMEOUT = 7
EXIT_LAUNCH_FAILED = 8

USAGE = """Usage: start-desktop.cmd [options]
  -Project <path>            open this project entry file (.nproj) after start
  -QaExitAfterSeconds <sec>  timed QA run: wait for the app, then exit with its code
  -RenderLog <path>          record the Qt Quick render backend log
  -PrepareOnly               run the startup checks, do not open the window
  -AllowLowIntegrity         allow starting from a Low integrity process (QA only)
The same options accept the POSIX spelling (--project, --prepare-only, ...)."""

# 解释器自检用的探针代码：写在一处，测试与日志都引用这些常量，不各写一份。
BASE_PREFIX_PROBE = "import json,sys; print(json.dumps(sys.base_prefix))"
VERSION_PROBE = "import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))"
IDENTITY_PROBE = (
    "import json,sys,struct,platform,sysconfig; "
    "print(json.dumps([list(sys.version_info[:3]),sys.implementation.cache_tag,"
    "struct.calcsize(chr(80)),platform.machine(),sysconfig.get_config_var('Py_GIL_DISABLED')]))"
)

# 重定向器：uv 的 launcher 会内嵌原始解释器路径、不认 pyvenv.cfg，必须换成 CPython 自带的那对。
REDIRECTORS = (("venvlauncher.exe", "python.exe"), ("venvwlauncher.exe", "pythonw.exe"))

_PYVENV_VERSION = re.compile(r"^\s*(version_info|version)\s*=\s*(\d+)\.(\d+)")
_PYVENV_REDIRECT = re.compile(r"^(home|executable|command)\s*=")
_QUOTED_VALUE = (" ", '"')


class UsageError(ValueError):
    """参数不认识或不合法：不猜，按用法错误退出。"""


class LaunchError(RuntimeError):
    """启动前置条件不满足：消息直接给用户看，退出码说明卡在哪一步。"""

    def __init__(self, message: str, *, exit_code: int = EXIT_RUNTIME_PREPARE_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class StartedApp(Protocol):
    """起起来的应用：只需要 pid 与一个可等待的退出码（测试换成假的即可）。"""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int | None: ...


# --- 参数 ------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchOptions:
    """一次启动的全部意图；两种拼写（``-Project`` / ``--project``）解析后完全一样。"""

    project: str | None = None
    qa_exit_after_seconds: float = 0.0
    render_log: str | None = None
    prepare_only: bool = False
    allow_low_integrity: bool = False


_VALUE_OPTIONS = {
    "project": "project",
    "qaexitafterseconds": "qa_exit_after_seconds",
    "renderlog": "render_log",
}
_SWITCH_OPTIONS = {"prepareonly": "prepare_only", "allowlowintegrity": "allow_low_integrity"}
_HELP_OPTIONS = {"help", "h", "?"}


def option_key(name: str) -> str:
    """``-QaExitAfterSeconds`` / ``--qa-exit-after-seconds`` / ``-qa_exit`` 归一化成一个键。

    PowerShell 的参数名与 POSIX 风格只差大小写和分隔符，这里把连字符、下划线与前导
    横线一并去掉再小写，两种拼写自然落到同一个键上，不必维护两张表。
    """
    return name.lstrip("-").replace("-", "").replace("_", "").lower()


def parse_options(argv: Sequence[str]) -> LaunchOptions:
    """解析启动参数；不认识／不合法就抛 :class:`UsageError`（调用方按退出码 2 处理）。"""
    tokens = list(argv)
    values: dict[str, str] = {}
    switches: dict[str, bool] = {}
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        index += 1
        if not raw.startswith("-") or not raw.lstrip("-"):
            raise UsageError(f"unexpected argument: {raw}")
        name, has_inline, inline = raw.partition("=")
        key = option_key(name)
        if key in _SWITCH_OPTIONS:
            if has_inline:
                raise UsageError(f"{name} is a switch and takes no value")
            switches[_SWITCH_OPTIONS[key]] = True
            continue
        if key not in _VALUE_OPTIONS:
            raise UsageError(f"unknown option: {name}")
        if has_inline:
            values[_VALUE_OPTIONS[key]] = inline
            continue
        if index >= len(tokens):
            raise UsageError(f"{name} needs a value")
        values[_VALUE_OPTIONS[key]] = tokens[index]
        index += 1
    project = values.get("project")
    return LaunchOptions(
        project=project if project and project.strip() else None,
        qa_exit_after_seconds=_seconds_option(values.get("qa_exit_after_seconds")),
        render_log=values.get("render_log") or None,
        prepare_only=switches.get("prepare_only", False),
        allow_low_integrity=switches.get("allow_low_integrity", False),
    )


def _seconds_option(raw: str | None) -> float:
    if raw is None:
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        raise UsageError(f"-QaExitAfterSeconds needs a number, got {raw!r}") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise UsageError(f"-QaExitAfterSeconds needs a positive number, got {raw!r}")
    return seconds


def help_requested(argv: Sequence[str]) -> bool:
    return any(option_key(token) in _HELP_OPTIONS for token in argv if token.startswith("-"))


# --- 工程布局 --------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectPaths:
    """工程内几条固定路径：启动器只认这些，别处不许再拼一遍。"""

    root: Path
    python: Path
    pythonw: Path
    config: Path
    runtime: Path
    launch_log: Path


def project_paths(root) -> ProjectPaths:
    root = Path(root).resolve()
    return ProjectPaths(
        root=root,
        python=root / ".venv" / "Scripts" / "python.exe",
        pythonw=root / ".venv" / "Scripts" / "pythonw.exe",
        config=root / ".venv" / "pyvenv.cfg",
        runtime=root / ".runtime" / "python",
        launch_log=root / ".runtime" / "logs" / "launch.log",
    )


def venv_problem(paths: ProjectPaths) -> str | None:
    """虚拟环境不全就直说缺什么：这是「先跑 setup-environment.cmd」的信号。"""
    missing = [str(path) for path in (paths.python, paths.pythonw, paths.config) if not path.is_file()]
    if not missing:
        return None
    return (
        "The project virtual environment is incomplete (missing: "
        + ", ".join(missing)
        + "). Run setup-environment.cmd first, or uv sync --locked --all-packages --all-extras --all-groups."
    )


def runtime_problem(runtime: Path) -> str | None:
    """本地运行时要同时有解释器与标准库，半截的运行时不覆盖、直接报。"""
    missing = [str(path) for path in (runtime / "python.exe", runtime / "Lib" / "encodings") if not path.exists()]
    if not missing:
        return None
    return (
        "Project runtime is incomplete (missing: "
        + ", ".join(missing)
        + "). If it is a failed new installation, move .runtime/python aside and rerun setup-environment.cmd."
    )


def pyvenv_version(text: str) -> tuple[int, int] | None:
    """``pyvenv.cfg`` 里声明的 Python 版本；没有就返回 None（=不匹配）。"""
    for line in text.splitlines():
        match = _PYVENV_VERSION.match(line)
        if match:
            return int(match.group(2)), int(match.group(3))
    return None


def repointed_pyvenv_text(text: str, runtime: Path) -> str:
    """把 ``home`` 重指到工程内运行时（搬过工程的 venv 不许再指旧机器）。

    幂等：内容已经正确时返回值与原文本逐字相同，调用方据此决定要不要落盘。
    """
    kept = [line for line in text.splitlines() if not _PYVENV_REDIRECT.match(line)]
    return "\n".join([f"home = {runtime}", *kept]) + "\n"


def repoint_pyvenv(paths: ProjectPaths, runtime: Path) -> bool:
    """需要时重写 ``pyvenv.cfg``；返回是否真的改了文件。"""
    text = paths.config.read_text(encoding="utf-8")
    desired = repointed_pyvenv_text(text, runtime)
    if text == desired:
        return False
    paths.config.write_bytes(desired.encode("utf-8"))
    return True


def redirector_pairs(paths: ProjectPaths) -> list[tuple[Path, Path]]:
    """``(CPython 自带重定向器, 工程内 venv 可执行文件)`` 两对。"""
    return [
        (paths.runtime / "Lib" / "venv" / "scripts" / "nt" / redirector,
         paths.root / ".venv" / "Scripts" / target)
        for redirector, target in REDIRECTORS
    ]


def redirectors_mismatched(
    pairs: Sequence[tuple[Path, Path]],
    *,
    read_bytes: Callable[[Path], bytes] = Path.read_bytes,
) -> list[str]:
    """逐对比 SHA-256，返回不一致的可执行文件名；缺文件是错误而不是「不一致」。"""
    mismatched: list[str] = []
    for redirector, installed in pairs:
        if not redirector.is_file():
            raise LaunchError(
                f"Missing CPython redirector: {redirector}",
                exit_code=EXIT_VENV_INCOMPLETE,
            )
        if not installed.is_file():
            raise LaunchError(
                f"Missing virtual environment executable: {installed}",
                exit_code=EXIT_VENV_INCOMPLETE,
            )
        if hashlib.sha256(read_bytes(redirector)).digest() != hashlib.sha256(read_bytes(installed)).digest():
            mismatched.append(installed.name)
    return mismatched


# --- 解释器边界 ------------------------------------------------------------------


class Interpreter:
    """一个解释器 + 一个可注入的进程边界：测试只换 ``run``，不真起进程。"""

    def __init__(self, executable, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.executable = Path(executable)
        self._run = run

    def _call(self, arguments: Sequence[str]) -> subprocess.CompletedProcess:
        return self._run([str(self.executable), *arguments], capture_output=True, text=True, check=False)

    def base_prefix(self) -> Path:
        result = self._call(["-I", "-c", BASE_PREFIX_PROBE])
        if result.returncode != 0:
            raise LaunchError("Could not locate the virtual environment base Python." + tail(result))
        try:
            return Path(json.loads((result.stdout or "").strip()))
        except (ValueError, OSError) as exc:
            raise LaunchError(f"Could not parse the base Python prefix: {exc}") from exc

    def version(self) -> tuple[int, int]:
        result = self._call(["-I", "-c", VERSION_PROBE])
        parts = (result.stdout or "").strip().replace(" ", ".").split(".")
        if result.returncode != 0 or len(parts) < 2:
            raise LaunchError("Could not inspect the project-local Python runtime." + tail(result))
        try:
            return int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise LaunchError(f"Could not parse the runtime version: {exc}") from exc

    def identity(self) -> list:
        result = self._call(["-I", "-c", IDENTITY_PROBE])
        if result.returncode != 0:
            raise LaunchError("Cannot verify the existing virtual environment interpreter."
                              " Recreate .venv with uv and rerun environment setup." + tail(result))
        try:
            return list(json.loads((result.stdout or "").strip()))
        except (ValueError, TypeError) as exc:
            raise LaunchError(f"Could not parse the interpreter identity: {exc}") from exc

    def script(self, script: Path, *extra: str) -> subprocess.CompletedProcess:
        return self._call(["-I", str(script), *extra])


def tail(result: subprocess.CompletedProcess, limit: int = 20) -> str:
    """把子进程输出接在报错后面：诊断信息不该因为被捕获就丢掉。"""
    text = "\n".join(part for part in ((result.stdout or "").strip(), (result.stderr or "").strip()) if part)
    if not text:
        return ""
    lines = text.splitlines()[-limit:]
    return "\n" + "\n".join(lines)


# --- 启动取证 --------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessSnapshot:
    """父进程现场：pid、完整性、映像名、命令行（取不到的字段如实写 unknown）。"""

    pid: int
    integrity: int | None = None
    image: str = "unknown"
    command_line: str = "unknown"


def integrity_text(level: int | None) -> str:
    return "unknown" if level is None else f"0x{level:04x}"


def current_integrity() -> int | None:
    """本进程的完整性级别；读不到就返回 None（不拦、不猜，日志写 unknown）。"""
    try:
        return runtime_security.current_integrity_level()
    except OSError:
        return None


def launch_decision(level: int | None, *, allow: bool) -> str:
    """纯判定：这次启动是正常、显式放行，还是被硬拦。"""
    if not runtime_security.low_integrity_blocks(level):
        return "normal"
    return "allow_low_integrity" if allow else "blocked"


def low_integrity_allowed(options: LaunchOptions, environ: Mapping[str, str]) -> bool:
    """``-AllowLowIntegrity`` 或环境变量，两条路都算显式放行。"""
    return options.allow_low_integrity or runtime_security.allow_low_integrity_requested(environ)


def _decision_reason(decision: str) -> str | None:
    """日志里的 ``reason=``：正常启动不写（与旧脚本的字段一致）。"""
    return {"blocked": "low_integrity", "allow_low_integrity": "explicit_override"}.get(decision)


def format_launch_fields(fields: Mapping[str, object]) -> str:
    """``launch.log`` 的一行：``key=value``，含空格或引号的值加引号，bool 写 True/False。"""
    parts: list[str] = []
    for key, value in fields.items():
        if isinstance(value, str):
            text = value
        elif isinstance(value, bool):
            text = "True" if value else "False"
        else:
            text = str(value)
        parts.append(f'{key}="{text}"' if any(marker in text for marker in _QUOTED_VALUE) else f"{key}={text}")
    return " ".join(parts)


def launch_log_fields(
    *,
    pid: int,
    integrity: int | None,
    parent: ProcessSnapshot | None,
    allow_low_integrity: bool,
    command_line: str,
    decision: str,
    reason: str | None = None,
) -> dict[str, object]:
    """一次启动的固定字段：出问题时靠它回答「谁启动的、父进程什么完整性、怎么判的」。"""
    parent = parent or ProcessSnapshot(pid=0)
    fields: dict[str, object] = {
        "pid": pid,
        "self_integrity": integrity_text(integrity),
        "parent_pid": parent.pid,
        "parent_integrity": integrity_text(parent.integrity),
        "parent_image": parent.image,
        "parent_cmd": parent.command_line,
        "allow_low_integrity": allow_low_integrity,
        "cmdline": command_line,
        "decision": decision,
    }
    if reason is not None:
        fields["reason"] = reason
    return fields


class LaunchLog:
    """``.runtime/logs/launch.log``：写不进去不能拖累启动，所以失败也算成功。"""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = datetime.now) -> None:
        self.path = Path(path)
        self._clock = clock

    def write(self, text: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{self._clock().astimezone().isoformat(timespec='seconds')} {text}\n")
        except OSError:
            pass


# --- 进程现场 --------------------------------------------------------------------


def current_command_line() -> str:
    """本进程的完整命令行（``GetCommandLineW``）；其它平台退回可执行文件 + argv。"""
    if sys.platform != "win32":
        return " ".join([sys.executable, *sys.argv])
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
        return kernel32.GetCommandLineW() or ""
    except (OSError, AttributeError):
        return " ".join([sys.executable, *sys.argv])


def current_parent_pid() -> int:
    """当前进程的父进程 pid；取不到返回 0（日志里就写 0，不猜）。"""
    if sys.platform != "win32":
        return os.getppid()
    pid = os.getpid()
    for entry in _toolhelp_processes():
        if entry[0] == pid:
            return entry[1]
    return 0


def process_snapshot(pid: int) -> ProcessSnapshot | None:
    """父进程 pid／映像名：只查身份，不读对方内存。

    命令行这里如实写 ``unknown``：``GetCommandLineW`` 只给本进程，读对方的 PEB 要按
    架构与系统版本写死偏移（错了会读坏内存），``wmic`` 在新 Windows 上已被移除。
    父进程的 pid + 映像 + 完整性已经足够判断是不是令牌继承。
    """
    if sys.platform != "win32" or pid <= 0:
        return None
    try:
        image = _process_image(pid) or "unknown"
        return ProcessSnapshot(
            pid=pid,
            integrity=runtime_security.process_integrity_level(pid),
            image=image,
        )
    except OSError:
        return None


def _toolhelp_processes() -> list[tuple[int, int, str]]:
    """``(pid, 父进程 pid, 映像名)`` 快照；用 Toolhelp，不依赖任何命令行工具。"""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot:
        return []
    entries: list[tuple[int, int, str]] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return entries
        while True:
            entries.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile)))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return entries
    finally:
        kernel32.CloseHandle(snapshot)


def _process_image(pid: int) -> str | None:
    """另一个进程的完整映像路径（``QueryFullProcessImageNameW``，同用户够用）。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return str(buffer.value) or None
    finally:
        kernel32.CloseHandle(handle)


# --- 界面进程 --------------------------------------------------------------------


def native_app_arguments(options: LaunchOptions, root: Path) -> list[str]:
    """``pythonw`` 的完整参数：``-I -m narrative.native_app`` 加上透传的启动选项。

    ``--render-log`` 的相路径按工程根解析（与旧脚本 ``Join-Path`` 的行为一致）；
    ``--project`` 原样透传，由应用自己解释用户给的路径。
    """
    arguments = ["-I", "-m", "narrative.native_app"]
    if options.project:
        arguments += ["--project", options.project]
    if options.qa_exit_after_seconds > 0:
        arguments += ["--qa-exit-after-seconds", seconds_text(options.qa_exit_after_seconds)]
    if options.render_log:
        render_log = Path(options.render_log)
        if not render_log.is_absolute():
            render_log = root / render_log
        arguments += ["--render-log", str(render_log.resolve())]
    return arguments


def seconds_text(value: float) -> str:
    """``5.0`` 写成 ``5``、``0.5`` 写成 ``0.5``：与旧脚本的不变文化格式化一致。"""
    return f"{value:g}"


def spawn_native_app(
    paths: ProjectPaths,
    arguments: Sequence[str],
    *,
    popen: Callable[..., StartedApp] = subprocess.Popen,
) -> StartedApp:
    """用 ``pythonw`` 起界面并立刻返回：pythonw 没有控制台，窗口归它自己。"""
    return popen([str(paths.pythonw), *arguments], cwd=str(paths.root))


def blocked_message(
    integrity: int | None,
    parent: ProcessSnapshot | None,
    *,
    launch_log: Path,
) -> list[str]:
    """低完整性硬拦时的说明：为什么、已经自动试过什么、现在该怎么办。"""
    parent = parent or ProcessSnapshot(pid=0)
    return [
        "=" * 62,
        "BLOCKED: this process is Low integrity, so the app could not save anything.",
        "",
        f"  integrity:            {integrity_text(integrity)} (Low; Medium is required)",
        f"  parent process:       {parent.image} (pid {parent.pid}, integrity {integrity_text(parent.integrity)})",
        f"  parent command line:  {parent.command_line}",
        "",
        "Why it happens (two separate causes, both real):",
        "  a) the interpreter files carry a Low integrity label - Windows starts any",
        "     Low-labelled exe as Low, even from a Medium parent (Explorer included).",
        "     Such a process cannot write %USERPROFILE%\\.narrative, so recent projects",
        "     and model settings silently fail (SQLite: \"attempt to write a readonly",
        "     database\").",
        "  b) this window itself was started from a restricted terminal / sandbox /",
        "     agent shell, which passes its Low token down to cmd -> python.",
        "",
        "What was already tried automatically:",
        "  the interpreter label fix ran before this point - see the integrity_label",
        f"  lines in {launch_log}",
        "  Relaunching through explorer.exe is NOT attempted: it does not help when the",
        "  exe itself carries the Low label, it only starts the app twice.",
        "",
        "What to do:",
        "  1. Close this window and double-click start-desktop.cmd in Explorer (or use",
        "     the Start menu / desktop shortcut).",
        "  2. Debugging from a restricted terminal on purpose? Pass -AllowLowIntegrity",
        f"     (or set {runtime_security.ALLOW_LOW_INTEGRITY_ENV}=1). The app will then",
        "     keep a permanent warning in its status bar.",
        "",
        f"  launch log: {launch_log}",
        "=" * 62,
    ]


# --- 一次启动 --------------------------------------------------------------------


class Launcher:
    """把一次启动拆成「参数 → 环境 → 取证 → 起界面」四步，进程与探测都可注入。"""

    def __init__(
        self,
        *,
        root: Path | None = None,
        environ: MutableMapping[str, str] | None = None,
        run_process: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        spawner: Callable[[ProjectPaths, Sequence[str]], StartedApp] = spawn_native_app,
        integrity_probe: Callable[[], int | None] = current_integrity,
        snapshot_of: Callable[[int], ProcessSnapshot | None] = process_snapshot,
        parent_of: Callable[[], int] = current_parent_pid,
        command_line: Callable[[], str] = current_command_line,
        clock: Callable[[], datetime] = datetime.now,
        out: Callable[[str], None] = print,
        err_out: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = project_paths(Path(__file__).resolve().parents[2] if root is None else root)
        self._environ: MutableMapping[str, str] = os.environ if environ is None else environ
        self._run_process = run_process
        self._spawner = spawner
        self._integrity_probe = integrity_probe
        self._snapshot_of = snapshot_of
        self._parent_of = parent_of
        self._command_line = command_line
        self._out = out
        self._err = err_out or (lambda text: print(text, file=sys.stderr))
        self._pid = os.getpid()
        self.log = LaunchLog(self.paths.launch_log, clock=clock)

    # 入口 ------------------------------------------------------------------

    def run(self, argv: Sequence[str]) -> int:
        if help_requested(argv):
            self._out(USAGE)
            return EXIT_OK
        try:
            options = parse_options(argv)
        except UsageError as exc:
            self._err(f"Invalid arguments: {exc}")
            self._err(USAGE)
            return EXIT_USAGE
        problem = venv_problem(self.paths)
        if problem is not None:
            self._err(problem)
            return EXIT_VENV_INCOMPLETE
        try:
            self._prepare_environment()
        except LaunchError as exc:
            self._err(str(exc))
            return exc.exit_code
        if options.prepare_only:
            self._out("Project environment is ready.")
            return EXIT_OK
        return self._start(options)

    # 环境 ------------------------------------------------------------------

    def _prepare_environment(self) -> None:
        """照旧脚本的顺序：先修标签，再准备运行时，其次重定向器与 pyvenv，最后自检。"""
        self._repair_labels("pre")
        if not self.paths.runtime.is_dir():
            self._prepare_runtime(self._venv_interpreter().base_prefix())
        problem = runtime_problem(self.paths.runtime)
        if problem is not None:
            raise LaunchError(problem, exit_code=EXIT_VENV_INCOMPLETE)
        self._ensure_redirectors()
        repoint_pyvenv(self.paths, self.paths.runtime)
        self._run_startup_checks()

    def _venv_interpreter(self) -> Interpreter:
        return Interpreter(self.paths.python, run=self._run_process)

    def _runtime_interpreter(self) -> Interpreter:
        return Interpreter(self.paths.runtime / "python.exe", run=self._run_process)

    def _repair_labels(self, stage: str) -> None:
        """解释器树带 Low 标签时的自愈：判定与 icacls 只在 check_runtime.py 里，这里只调用与记账。"""
        script = self.paths.root / "scripts" / "check_runtime.py"
        if not script.is_file():
            self.log.write(f"label_stage={stage} result=missing_script path={script}")
            return
        result = self._venv_interpreter().script(script, "--fix-integrity-labels")
        for line in (result.stdout or "").splitlines():
            if "integrity_label " in line:
                self.log.write(f"label_stage={stage} {line.strip()}")
        if result.returncode == 0:
            return
        self.log.write(f"label_stage={stage} result=failed exit_code={result.returncode}")
        # 修不动是「要说出来的失败」，不是「中止启动」：低完整性进程抬不高标签。
        self._out("Warning: could not raise the interpreter integrity label to Medium.")
        self._out("A Low-labelled interpreter cannot be fixed from a Low process.")
        self._out("Run this again from a normal (Medium) terminal.")
        self._echo(result)

    def _prepare_runtime(self, source: Path) -> None:
        self._out("Preparing the project-local Python runtime for first launch.")
        self._run_script(
            Interpreter(source / "python.exe", run=self._run_process),
            self.paths.root / "scripts" / "localize_python.py",
            "--source", str(source),
        )

    def _ensure_redirectors(self) -> None:
        """venv 的重定向器与本地运行时不一致（uv 重建过环境）就重整一次。"""
        runtime = self._runtime_interpreter()
        if pyvenv_version(self.paths.config.read_text(encoding="utf-8")) != runtime.version():
            raise LaunchError(
                "The virtual environment and local Python runtime versions do not match."
                " Inspect them before rebuilding the runtime.",
                exit_code=EXIT_VENV_INCOMPLETE,
            )
        if not redirectors_mismatched(redirector_pairs(self.paths)):
            return
        if runtime.identity() != self._venv_interpreter().identity():
            raise LaunchError(
                "The virtual environment and local runtime differ in Python version, ABI, or"
                " architecture. Inspect the existing runtime before rebuilding it.",
                exit_code=EXIT_VENV_INCOMPLETE,
            )
        self._out("Repairing virtual environment launchers for the project-local Python runtime.")
        self._run_script(
            runtime,
            self.paths.root / "scripts" / "localize_python.py",
            "--source", str(self.paths.runtime),
        )

    def _run_startup_checks(self) -> None:
        result = self._venv_interpreter().script(self.paths.root / "scripts" / "check_runtime.py")
        self._echo(result)
        if result.returncode != 0:
            raise LaunchError(
                "Project startup checks failed. See the diagnostic above.",
                exit_code=EXIT_STARTUP_CHECKS_FAILED,
            )

    def _run_script(self, interpreter: Interpreter, script: Path, *extra: str) -> None:
        result = interpreter.script(script, *extra)
        self._echo(result)
        if result.returncode != 0:
            raise LaunchError("Project runtime preparation failed. See the diagnostic above.")

    # 取证与启动 --------------------------------------------------------------

    def _start(self, options: LaunchOptions) -> int:
        allow = low_integrity_allowed(options, self._environ)
        integrity = self._integrity_probe()
        parent_pid = self._parent_of()
        parent = self._snapshot_of(parent_pid) if parent_pid > 0 else None
        decision = launch_decision(integrity, allow=allow)
        fields = launch_log_fields(
            pid=self._pid,
            integrity=integrity,
            parent=parent,
            allow_low_integrity=allow,
            command_line=self._command_line(),
            decision=decision,
            reason=_decision_reason(decision),
        )
        if decision == "blocked":
            self.log.write(format_launch_fields(fields))
            for line in blocked_message(integrity, parent, launch_log=self.paths.launch_log):
                self._out(line)
            return EXIT_LOW_INTEGRITY_BLOCKED
        if decision == "allow_low_integrity":
            self.log.write(format_launch_fields(fields))
            # 子进程要继承这个环境变量：应用侧据此挂常驻提示而不是硬失败。
            self._environ[runtime_security.ALLOW_LOW_INTEGRITY_ENV] = "1"
            self._out("Warning: starting with Low integrity on purpose (-AllowLowIntegrity).")
            self._out("The app will keep showing that nothing is written to the user directory.")
        else:
            self.log.write(format_launch_fields(fields))
        # 启动前再修一次标签：上面的重定向器修复会在低完整性进程里重写 exe，新写的又会被打回 Low。
        self._repair_labels("prelaunch")
        arguments = native_app_arguments(options, self.paths.root)
        self._out(f"Starting native desktop from {self.paths.root}.")
        try:
            app = self._spawner(self.paths, arguments)
        except OSError as exc:
            self._err(f"Could not start the desktop app: {exc}")
            return EXIT_LAUNCH_FAILED
        self._out(f"Native desktop started (PID {app.pid}).")
        if options.qa_exit_after_seconds <= 0:
            return EXIT_OK
        return self._wait_for_qa(options, app, fields)

    def _wait_for_qa(self, options: LaunchOptions, app: StartedApp, fields: Mapping[str, object]) -> int:
        try:
            app.wait(timeout=options.qa_exit_after_seconds + 15)
        except subprocess.TimeoutExpired:
            self._err("Timed desktop QA did not exit after the requested duration.")
            return EXIT_QA_TIMEOUT
        code = app.returncode
        code_text = "unknown" if code is None else str(code)
        self._out(f"Native desktop QA exited (code {code_text}).")
        exit_fields = dict(fields)
        exit_fields["decision"] = "qa_exit"
        exit_fields["qa_exit_code"] = code_text
        self.log.write(format_launch_fields(exit_fields))
        # QA 模式把应用的退出码原样交回去：应用侧拦下低完整性时是 3，外部脚本据此判断成败。
        return code if isinstance(code, int) and code != 0 else EXIT_OK

    def _echo(self, result: subprocess.CompletedProcess) -> None:
        """子进程的输出要给人看：被捕获不等于要吞掉。"""
        if result.stdout:
            self._out(result.stdout.rstrip())
        if result.stderr:
            self._err(result.stderr.rstrip())


def main(argv: Sequence[str] | None = None) -> int:
    return Launcher().run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
