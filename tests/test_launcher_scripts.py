"""启动链的守卫：三个批处理入口必须能在没有 PowerShell 的机器上工作。

现场教训：PowerShell 7 是从 Microsoft Store 装的，别名落在
``%LOCALAPPDATA%\\Microsoft\\WindowsApps``；Explorer 会缓存环境变量，别名的 PATH 条目在
某些会话里根本没有（沙箱、计划任务、代理 shell 也一样），于是只查 PATH 的脚本会喊
"pwsh was not found"，而它明明装着。绕了几轮绝对路径之后结论更简单：启动链不需要
PowerShell——逻辑都在 ``narrative.launcher`` 里，``.cmd`` 只找工程内的 python.exe。
这批断言就是防止有人再把 PowerShell（或它带来的 PATH 猜测）写回来。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_SCRIPTS = ["start-desktop.cmd", "start.cmd", "setup-environment.cmd"]
FORBIDDEN = ("pwsh", "powershell", "where pwsh", "executionpolicy")


def read_bytes(name: str) -> bytes:
    return (ROOT / name).read_bytes()


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_never_mentions_powershell(name):
    text = read_bytes(name).decode("ascii")
    lowered = text.lower()
    for token in FORBIDDEN:
        assert token not in lowered, f"{name} 里出现了 {token!r}：启动链不许再依赖 PowerShell"


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_is_pure_ascii_and_crlf(name):
    """cmd.exe 按 GBK 解析批处理：UTF-8 的中文注释会变成乱码，混排行尾会吞掉命令。"""
    raw = read_bytes(name)
    raw.decode("ascii")
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} 必须是纯 CRLF"
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} 不该带 BOM"


def test_start_desktop_calls_the_python_launcher():
    text = read_bytes("start-desktop.cmd").decode("ascii")
    assert r".venv\Scripts\python.exe" in text, "必须用工程内的解释器，不用 PATH 上碰巧的那个"
    assert "-m narrative.launcher" in text
    assert "-I" in text, "启动器也要隔离模式：不读用户 site、不吃外部 PYTHONPATH"
    # 参数原样透传：-Project / -QaExitAfterSeconds / -RenderLog / -PrepareOnly 都靠它。
    assert "%*" in text
    assert "setup-environment.cmd" in text, "缺 venv 时要告诉用户先跑哪一步"


def test_start_cmd_is_only_a_forwarder():
    text = read_bytes("start.cmd").decode("ascii")
    assert "start-desktop.cmd" in text, "start.cmd 只转调，不许另写一份启动逻辑"
    assert "%*" in text, "参数要透传"
    assert "errorlevel" in text, "退出码要原样交回去"


@pytest.mark.parametrize("name", ["start-desktop.cmd", "setup-environment.cmd"])
def test_entry_script_pauses_only_on_failure(name):
    """成功时窗口该关掉；失败时才停下来让人看见错误（与旧脚本行为一致）。"""
    text = read_bytes(name).decode("ascii")
    assert "pause" in text
    failure_block = text[text.index("pause") - 300 :]
    assert re.search(r'if not "%narrative_exit_code%"=="0"|exit /b 1|goto :failed', failure_block)


def test_setup_environment_syncs_then_prepares():
    text = read_bytes("setup-environment.cmd").decode("ascii")
    assert "uv sync --locked --all-packages --all-extras --all-groups" in text
    assert "-m narrative.launcher -PrepareOnly" in text, "准备与自检复用启动器，不另写一套"
    assert "check_runtime.py" in text and "--fix-integrity-labels" in text


def test_no_windows_powershell_entry_points_remain():
    """两个真源比没有更糟：删掉的启动脚本不许被悄悄加回来。"""
    for name in ("start-desktop.ps1", "start.ps1", "setup-environment.ps1"):
        assert not (ROOT / name).exists(), f"{name} 应该已经被 start-desktop.cmd + launcher.py 取代"
    assert not (ROOT / "scripts" / "fix_venv_integrity.ps1").exists(), (
        "标签修复只在 scripts/check_runtime.py --fix-integrity-labels 里"
    )
