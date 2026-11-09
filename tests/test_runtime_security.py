"""Startup diagnoses an abnormal Low runtime but only stops when writes really fail."""

import runpy
import sys
from pathlib import Path

import pytest

from narrative import runtime_security


def test_low_runtime_warns_but_starts_when_project_is_writable(monkeypatch, capsys):
    monkeypatch.setattr(runtime_security, "current_integrity_level", lambda: 0x1000)
    script = Path(__file__).resolve().parents[1] / "scripts/check_runtime.py"
    runpy.run_path(str(script), run_name="__main__")
    message = capsys.readouterr().err
    assert "Low" in message and "Medium" in message


def test_low_runtime_stops_when_project_write_probe_fails(monkeypatch, capsys):
    monkeypatch.setattr(runtime_security, "current_integrity_level", lambda: 0x1000)
    real_write = Path.write_text

    def guarded(self, *args, **kwargs):
        if ".runtime-write-probe" in str(self):
            raise OSError("denied")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded)
    script = Path(__file__).resolve().parents[1] / "scripts/check_runtime.py"
    with pytest.raises(SystemExit) as failed:
        runpy.run_path(str(script), run_name="__main__")
    assert failed.value.code == 1


@pytest.mark.parametrize("level", [None, 0x2000, 0x3000])
def test_normal_integrity_is_allowed(monkeypatch, level):
    monkeypatch.setattr(runtime_security, "current_integrity_level", lambda: level)
    runtime_security.require_medium_integrity()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process token")
def test_current_windows_process_has_normal_user_integrity():
    level = runtime_security.current_integrity_level()
    if level < runtime_security.MEDIUM_INTEGRITY:
        pytest.skip("当前会话本身是 Low 完整性（沙箱/受限终端），本用例只验证正常桌面启动")
    assert level >= runtime_security.MEDIUM_INTEGRITY
