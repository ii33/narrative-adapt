"""启动脚本必须能在"PATH 里没有 pwsh"的情况下也找到它。

现场教训：PowerShell 7 是从 Microsoft Store 装的，别名落在
``%LOCALAPPDATA%\\Microsoft\\WindowsApps``；Explorer 会缓存环境变量，别名的 PATH 条目在
某些会话里根本没有（沙箱、计划任务、代理 shell 也一样），于是只查 PATH 的脚本会喊
"pwsh was not found"，而它明明装着。脚本因此必须再试几个确定的绝对路径。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_SCRIPTS = ["start-desktop.cmd", "start.cmd", "setup-environment.cmd"]


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_does_not_trust_path_alone(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert "%~$PATH:P" in text, "先按 PATH 找一次"
    assert r"%ProgramFiles%\PowerShell\7\pwsh.exe" in text, "官方安装位置"
    assert r"%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" in text, "Store 别名的绝对路径"
    assert '"%PWSH_EXE%"' in text, "最终要用解析到的绝对路径启动，而不是再碰一次 PATH"


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_never_falls_back_to_windows_powershell(name):
    text = (ROOT / name).read_text(encoding="utf-8").lower()
    assert "powershell.exe" not in text
    assert "#requires -version 5.1" not in text


def test_scripts_repoint_at_powershell_7():
    for path in (ROOT / "scripts").glob("*.ps1"):
        text = path.read_text(encoding="utf-8")
        assert "#requires -Version 5.1" not in text, f"{path.name} 还在要求 Windows PowerShell 5.1"
