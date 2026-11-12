"""进程完整性判定：低完整性不许静默降级，取不到时不许误拦。

事故背景：从受限终端/沙箱启动 → 子进程继承低完整性令牌 → 写不了
``%USERPROFILE%\\.narrative`` → SQLite 报 "attempt to write a readonly database"。
``pythonw`` 没有控制台，以前那句 ``Write-Output`` 警告用户永远看不到，于是「手动点
启动脚本却以低完整性起来」发生了不止一次。这里的判定是纯函数，能被直接断言。
"""

import logging
import os
import sys
from pathlib import Path

import pytest

from narrative import runtime_security


def test_low_integrity_refuses_startup_with_actionable_text():
    refusal = runtime_security.startup_refusal(0x1000, allow=False)
    assert refusal is not None
    assert "0x1000" in refusal
    assert "低完整性" in refusal
    # 「怎么办」必须写进这句话里，否则和以前那句看不见的警告没区别。
    assert "start-desktop.cmd" in refusal
    assert runtime_security.ALLOW_LOW_INTEGRITY_ENV in refusal


@pytest.mark.parametrize("level", [None, runtime_security.MEDIUM_INTEGRITY, 0x3000])
def test_medium_or_unknown_integrity_is_not_blocked(level):
    assert runtime_security.startup_refusal(level, allow=False) is None


def test_escape_hatch_allows_low_integrity_startup():
    assert runtime_security.startup_refusal(0x1000, allow=True) is None


def test_low_integrity_notice_text_and_silence():
    notice = runtime_security.low_integrity_notice(0x1000)
    assert notice == runtime_security.LOW_INTEGRITY_NOTICE
    assert "改动不会写进用户目录" in notice
    assert runtime_security.low_integrity_notice(runtime_security.MEDIUM_INTEGRITY) == ""
    assert runtime_security.low_integrity_notice(None) == ""


def test_allow_low_integrity_env_parsing():
    allow = runtime_security.allow_low_integrity_requested
    assert allow({runtime_security.ALLOW_LOW_INTEGRITY_ENV: "1"}) is True
    assert allow({runtime_security.ALLOW_LOW_INTEGRITY_ENV: " TRUE "}) is True
    assert allow({runtime_security.ALLOW_LOW_INTEGRITY_ENV: "0"}) is False
    assert allow({}) is False


def test_current_notice_follows_the_process_level(monkeypatch):
    monkeypatch.setattr(runtime_security, "current_integrity_level", lambda: 0x1000)
    assert runtime_security.current_integrity_notice() == runtime_security.LOW_INTEGRITY_NOTICE
    monkeypatch.setattr(runtime_security, "current_integrity_level",
                        lambda: runtime_security.MEDIUM_INTEGRITY)
    assert runtime_security.current_integrity_notice() == ""


def test_unknown_level_is_logged_and_never_blocks(monkeypatch, caplog):
    monkeypatch.setattr(runtime_security, "current_integrity_level", lambda: None)
    with caplog.at_level(logging.INFO, logger=runtime_security.__name__):
        assert runtime_security.current_integrity_notice() == ""
        assert runtime_security.startup_refusal(None, allow=False) is None
    assert any("完整性" in record.message for record in caplog.records)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process token")
def test_process_integrity_matches_the_current_process():
    own = runtime_security.current_integrity_level()
    reported = runtime_security.process_integrity_level(os.getpid())
    if own is None or reported is None:
        pytest.skip("本会话读不到完整性标签")
    assert reported == own


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process token")
def test_unreadable_process_reports_unknown_instead_of_raising():
    # 不存在的 PID 只能返回 None：查不到就是查不到，不能把启动脚本带崩。
    assert runtime_security.process_integrity_level(0x7FFFFFF0) is None


# --- 解释器完整性标签：事故的真因 ---------------------------------------------
#
# Windows 对带 Low 标签的 exe 一律以 Low 启动，父进程是不是 Medium 都没用。
# 标签是低完整性进程（代理会话里的 uv sync / setup-environment / localize_python）
# 创建这些文件时打上的，所以修法是把解释器树重新设成 Medium。

LOW_ICACLS = r"""
.venv\Scripts\python.exe NT AUTHORITY\SYSTEM:(I)(F)
                          Mandatory Label\Low Mandatory Level:(I)(NW)
"""
MEDIUM_ICACLS = r"""
.venv\Scripts\python.exe NT AUTHORITY\SYSTEM:(I)(F)
                          Mandatory Label\Medium Mandatory Level:(I)(NW)
"""


def test_icacls_low_label_detection():
    assert runtime_security.icacls_reports_low_label(LOW_ICACLS)
    assert not runtime_security.icacls_reports_low_label(MEDIUM_ICACLS)
    assert not runtime_security.icacls_reports_low_label("")
    # 只有「Mandatory Label」没有「Low」时不认：宁可漏报，也不误改别人的标签。
    assert not runtime_security.icacls_reports_low_label("Mandatory Label\\High Mandatory Level")


def test_integrity_targets_cover_both_interpreter_trees_and_key_exes():
    targets = runtime_security.integrity_label_targets(Path("/repo"))
    paths = [str(path) for path, _recursive in targets]
    assert str(Path("/repo") / ".venv") in paths
    assert str(Path("/repo") / ".runtime/python") in paths
    assert any(item.endswith("pythonw.exe") for item in paths)
    # 递归只给 .venv：.runtime\python 带完整 CPython 文档，全量 /T 会拖垮启动。
    recursive = {str(path): flag for path, flag in targets}
    assert recursive[str(Path("/repo") / ".venv")] is True
    assert recursive[str(Path("/repo") / ".runtime/python")] is False


def test_non_recursive_repair_touches_only_that_path():
    """目录只打自己、不加 /T：大运行时全量重打标签会让启动慢到不可接受。"""
    argv = runtime_security.integrity_label_command(Path("/x/.runtime/python"), recursive=False)
    assert "/T" not in argv
    # 目录用继承标记；文件只能用裸 Medium（icacls 对文件不接受 OI/CI）。
    assert "(OI)(CI)Medium" in argv
    file_argv = runtime_security.integrity_label_command(Path("/x/.venv/Scripts/pythonw.exe"),
                                                        directory=False)
    assert "Medium" in file_argv and "(OI)(CI)Medium" not in file_argv
    recursive = runtime_security.integrity_label_command(Path("/x/.venv"), recursive=True)
    assert "/T" in recursive


def test_integrity_label_status_is_read_only_and_reports_low(tmp_path):
    target = tmp_path / ".venv"
    target.mkdir()
    lines = runtime_security.integrity_label_status([target], run=lambda argv: LOW_ICACLS)
    assert lines == [f"integrity_label path={target} level=Low"]
    absent = runtime_security.integrity_label_status([tmp_path / "gone"], run=lambda argv: "")
    assert absent == [f"integrity_label path={tmp_path / 'gone'} level=absent"]


def test_integrity_summary_is_one_line_and_names_the_low_path(tmp_path):
    """启动输出只留一行：Medium 时不吵，Low 时点名到具体 exe。"""
    healthy = tmp_path / ".venv"
    healthy.mkdir()
    summary = runtime_security.integrity_label_summary([healthy], run=lambda argv: MEDIUM_ICACLS)
    assert len(summary) == 1 and "level=Medium" in summary[0]

    broken = tmp_path / "pythonw.exe"
    broken.write_text("", encoding="utf-8")
    summary = runtime_security.integrity_label_summary([broken], run=lambda argv: LOW_ICACLS)
    assert len(summary) == 1 and "level=Low" in summary[0] and str(broken) in summary[0]


@pytest.mark.skipif(sys.platform != "win32", reason="icacls 只在 Windows 上存在")
def test_low_label_is_repaired_once_and_reported(tmp_path):
    target = tmp_path / ".venv"
    target.mkdir()
    commands: list[list[str]] = []
    outputs = iter([LOW_ICACLS, "", MEDIUM_ICACLS])

    def fake_icacls(argv):
        commands.append(argv)
        return next(outputs)

    report = runtime_security.ensure_medium_labels([target], run=fake_icacls)
    assert report["low"] == [str(target)]
    assert report["repaired"] == [str(target)]
    assert report["failed"] == []
    # 一次读取、一次修复、一次复核；修复命令就是把 (OI)(CI)Medium 写下去。
    set_commands = [argv for argv in commands if "/setintegritylevel" in argv]
    assert len(set_commands) == 1
    assert set_commands[0][1] == str(target)
    assert "(OI)(CI)Medium" in set_commands[0]
    assert "status=repaired_low_to_medium" in "\n".join(runtime_security.format_label_report(report))


@pytest.mark.skipif(sys.platform != "win32", reason="icacls 只在 Windows 上存在")
def test_medium_label_is_left_alone(tmp_path):
    target = tmp_path / ".venv"
    target.mkdir()
    commands: list[list[str]] = []

    def fake_icacls(argv):
        commands.append(argv)
        return MEDIUM_ICACLS

    report = runtime_security.ensure_medium_labels([target], run=fake_icacls)
    assert report["low"] == [] and report["repaired"] == [] and report["failed"] == []
    assert not any("/setintegritylevel" in argv for argv in commands)
    assert "status=medium_or_higher" in "\n".join(runtime_security.format_label_report(report))


@pytest.mark.skipif(sys.platform != "win32", reason="icacls 只在 Windows 上存在")
def test_repair_that_does_not_stick_is_reported_not_hidden(tmp_path):
    target = tmp_path / ".venv"
    target.mkdir()
    outputs = iter([LOW_ICACLS, "", LOW_ICACLS])

    report = runtime_security.ensure_medium_labels([target], run=lambda argv: next(outputs))
    assert report["repaired"] == []
    assert report["failed"] and report["failed"][0][0] == str(target)
    assert "status=repair_failed" in "\n".join(runtime_security.format_label_report(report))


@pytest.mark.skipif(sys.platform != "win32", reason="icacls 只在 Windows 上存在")
def test_missing_interpreter_tree_is_skipped(tmp_path):
    report = runtime_security.ensure_medium_labels([tmp_path / "absent"], run=lambda argv: "")
    assert report["skipped"] == [str(tmp_path / "absent")]
    assert report["low"] == [] and report["failed"] == []


def test_check_runtime_fix_mode_runs_the_repair(monkeypatch, capsys):
    """启动脚本只调这一个入口：`--fix-integrity-labels` 必须真的去修并打印结果。"""
    import runpy

    from narrative import runtime_security

    calls: list[list] = []

    def fake_ensure(targets, *, run=None):
        calls.append(list(targets))
        first = str(list(targets)[0])
        return {"checked": [first], "low": [first], "repaired": [first],
                "failed": [], "skipped": []}

    monkeypatch.setattr(runtime_security, "ensure_medium_labels", fake_ensure)
    monkeypatch.setattr(
        runtime_security, "integrity_label_status",
        lambda targets, run=None: ["integrity_label path=.venv level=Medium"],
    )
    monkeypatch.setattr(sys, "argv", ["check_runtime.py", "--fix-integrity-labels"])
    script = Path(__file__).resolve().parents[1] / "scripts/check_runtime.py"
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "status=repaired_low_to_medium" in out
    # 修完还要再打一遍状态：常态可见，一眼能看出有没有复发。
    assert "level=Medium" in out
    assert calls and calls[0] and str(calls[0][0][0]).endswith(".venv")


def test_check_runtime_fix_mode_fails_loudly(monkeypatch, capsys):
    """修不动不能当成功：退出码非 0，并指向人工脚本。"""
    import runpy

    from narrative import runtime_security

    monkeypatch.setattr(
        runtime_security, "ensure_medium_labels",
        lambda targets, *, run=None: {"checked": [], "low": [], "repaired": [],
                                      "failed": [("x", "denied")], "skipped": []},
    )
    monkeypatch.setattr(runtime_security, "integrity_label_status", lambda targets, run=None: [])
    monkeypatch.setattr(sys, "argv", ["check_runtime.py", "--fix-integrity-labels"])
    script = Path(__file__).resolve().parents[1] / "scripts/check_runtime.py"
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")
    assert exit_info.value.code == 1
    assert "fix_venv_integrity.ps1" in capsys.readouterr().err
