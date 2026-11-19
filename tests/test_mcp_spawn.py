"""stdio 会话的启动路径：命令规范化与启动失败的错误翻译。

真起子进程，被启动的脚本用 tests/fake_mcp_server.py 复制到 tmp_path 的那一份
（要测的是「用户填的脚本路径」，不是 tests/ 里的固定位置）；不联网、不要 mcp SDK。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from narrative import mcp_client

FAKE = Path(__file__).with_name("fake_mcp_server.py")
WINDOWS = sys.platform == "win32"


def _copy_fake(directory: Path) -> Path:
    """把假服务器复制成一个「用户自己写的 .py 脚本」。"""
    target = directory / "fake_mcp_server.py"
    shutil.copyfile(FAKE, target)
    return target


def _stdio(command: str | Path, **changes) -> mcp_client.McpServer:
    data = {"name": "spawn", "transport": "stdio", "command": str(command), "timeout": 20}
    data.update(changes)
    return mcp_client.McpServer.from_dict(data)


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """配置路径指到 tmp_path，绝不碰真实用户目录。"""
    path = tmp_path / "mcp-servers.json"
    monkeypatch.setenv(mcp_client.CONFIG_ENV, str(path))
    mcp_client.close()
    yield path
    mcp_client.close()


# -- 命令规范化 -------------------------------------------------------------


def test_spawn_argv_rules(tmp_path):
    """后缀决定启动方式；参数永远跟在脚本后面，server 本身不被改。"""
    script = tmp_path / "server.py"
    py = _stdio(script, args=["--flag"])
    assert mcp_client.spawn_argv(py) == [sys.executable, str(script), "--flag"]
    # 用户从资源管理器复制来的带引号路径也认
    assert mcp_client.spawn_argv(_stdio(f'"{script}"')) == [sys.executable, str(script)]
    assert py.command == str(script)

    batch = tmp_path / "run.CMD"  # 后缀大小写不敏感
    assert mcp_client.spawn_argv(_stdio(batch, args=["--x"])) == [
        os.environ.get("COMSPEC") or "cmd.exe", "/c", str(batch), "--x"]

    ps1 = tmp_path / "tool.ps1"
    try:
        argv = mcp_client.spawn_argv(_stdio(ps1))
    except mcp_client.McpError as exc:
        # 机器上没装 PowerShell：必须明说找不到，不能瞎猜着执行
        assert "pwsh" in str(exc) and "启动失败" in str(exc)
    else:
        assert argv[0].lower().endswith(("pwsh", "pwsh.exe", "powershell", "powershell.exe"))
        assert argv[1:] == ["-NoProfile", "-File", str(ps1)]

    # .exe 与 npx/uvx 这类交给 PATH 解析的命令：原样执行
    exe = tmp_path / "tool.exe"
    assert mcp_client.spawn_argv(_stdio(exe, args=["-x"])) == [str(exe), "-x"]
    assert mcp_client.spawn_argv(_stdio("npx", args=["-y", "some-mcp"])) == [
        "npx", "-y", "some-mcp"]


def test_normalization_keeps_stored_command(tmp_path):
    """界面显示的命令必须还是用户写的那份，规范化只在执行时发生。"""
    script = _copy_fake(tmp_path)
    mcp_client.save_servers([_stdio(script)])

    loaded = mcp_client.load_servers()[0]
    assert loaded.command == str(script)
    assert mcp_client.spawn_argv(loaded)[0] == sys.executable
    assert mcp_client.load_servers()[0].command == str(script)  # 规范化不留痕迹


# -- 常见写法直接能跑 -------------------------------------------------------


def test_py_script_command_handshakes(tmp_path):
    """.py 直接填进 command：用当前解释器起，initialize + tools/list 全程可用。"""
    server = _stdio(_copy_fake(tmp_path))
    with mcp_client.McpSession(server) as session:
        session.initialize()
        assert [tool["name"] for tool in session.list_tools()] == ["echo", "write_note"]


@pytest.mark.skipif(not WINDOWS, reason=".cmd 只有 Windows 的 cmd 能跑")
def test_cmd_script_runs_through_cmd(tmp_path):
    """.cmd 通过 cmd /c 执行；批处理里再调 python 脚本。"""
    script = _copy_fake(tmp_path)
    if not (str(script).isascii() and str(sys.executable).isascii()):
        # cmd 按 ANSI 读批处理文件，非 ASCII 路径写进去会被读坏：这不是被测对象
        pytest.skip("路径含非 ASCII 字符，跳过 cmd 批处理用例")
    batch = tmp_path / "run_server.cmd"
    batch.write_bytes(f'@echo off\r\n"{sys.executable}" "{script}"\r\n'.encode("ascii"))

    server = _stdio(batch)
    with mcp_client.McpSession(server) as session:
        session.initialize()
        assert [tool["name"] for tool in session.list_tools()] == ["echo", "write_note"]


# -- 起不来时说什么 ---------------------------------------------------------


def test_missing_command_reports_path_and_not_found(tmp_path):
    missing = tmp_path / "gone" / "server.exe"
    with pytest.raises(mcp_client.McpError) as excinfo:
        mcp_client.McpSession(_stdio(missing)).start()

    message = str(excinfo.value)
    assert "spawn" in message and "启动失败" in message
    assert str(missing) in message and "找不到" in message


@pytest.mark.skipif(not WINDOWS, reason="WinError 193 是 Windows 的错误码")
def test_plain_text_command_explains_not_executable(tmp_path):
    """把 .json 当命令：说清「不是可执行程序」并指到 .py / .cmd，而不是甩 WinError 193。"""
    data = tmp_path / "server.json"
    data.write_text('{"name": "not-a-program"}\n', encoding="utf-8")

    with pytest.raises(mcp_client.McpError) as excinfo:
        mcp_client.McpSession(_stdio(data)).start()

    message = str(excinfo.value)
    assert "spawn" in message and "启动失败" in message
    assert "不是可执行程序" in message
    assert ".py" in message and ".cmd" in message
    # 原始错误码不是唯一信息
    assert len(message) > len("MCP 服务器 spawn 启动失败：[WinError 193]")


def test_spawn_failures_are_mcp_errors_not_os_errors(tmp_path):
    """任何起不来的情况都必须落在 McpError，不许把 OSError 抛给上层。

    真漏出 OSError 时 pytest.raises 会因为异常类型不对而失败，原始异常就在报告里。
    """
    for command in ("narrative-no-such-server", tmp_path / "missing" / "a.json"):
        with pytest.raises(mcp_client.McpError):
            mcp_client.McpSession(_stdio(command)).start()
