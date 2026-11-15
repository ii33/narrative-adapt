"""首次启动：准备本地 Python 运行时、检查隔离，然后才起界面。

这一层以前用 ``pwsh`` 跑 ``start-desktop.ps1``，而那个脚本已经不在了——启动逻辑现在
在 :mod:`narrative.launcher`，逐条判定（``-PrepareOnly``、运行时准备、pyvenv 重指、
重定向器哈希、低完整性硬拦）都由 ``tests/test_launcher.py`` 用注入边界的假进程覆盖，
默认门禁里跑得又快又稳。这里只留两件必须动真东西的事：

* ``scripts/localize_python.py`` 自己的行为（它复制整个 CPython，没法用假进程替）；
* 真实子进程里 ``.venv\\Scripts\\python.exe -I -m narrative.launcher`` 这条命令行确实
  能跑起来——``start-desktop.cmd`` 执行的就是它。
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop launcher")


def test_launcher_module_is_runnable_the_way_the_batch_entry_calls_it():
    """``start-desktop.cmd`` 只做一件事：``python.exe -I -m narrative.launcher %*``。

    所以这条命令行必须自己就能跑：``-I`` 下不吃外部 PYTHONPATH／用户 site，靠 editable
    安装在工程内的包解析模块。跑 ``-Help`` 是为了不碰环境、不起界面。
    """
    result = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-I", "-m", "narrative.launcher", "-Help"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: start-desktop.cmd" in result.stdout


def test_prepare_only_reports_the_environment_without_a_shell(tmp_path):
    """``-PrepareOnly`` 的契约：退出码 0、说「ready」、绝不起界面。

    这里跑真实工程（``.runtime/python`` 已就位），因为「工程内的解释器真的自洽」只能
    这么验；界面不会被拉起，命令本身也不写用户目录。
    """
    result = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-I", "-m", "narrative.launcher", "-PrepareOnly"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Project environment is ready." in result.stdout


def test_localization_shows_probe_import_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv").mkdir()
    (root / ".venv/pyvenv.cfg").write_text("home = old\n", encoding="utf-8")
    (root / ".venv/Scripts").mkdir()
    site_packages = root / ".venv/Lib/site-packages"
    site_packages.mkdir(parents=True)
    cached_pth = tmp_path / "cached.pth"
    cached_pth.write_text("cached original\n", encoding="utf-8")
    editable = site_packages / "_editable_impl_narrative_core.pth"
    os.link(cached_pth, editable)
    source = tmp_path / "python"
    (source / "Lib/encodings").mkdir(parents=True)
    (source / "DLLs").mkdir()
    launchers = source / "Lib/venv/scripts/nt"
    launchers.mkdir(parents=True)
    for name, target in (("venvlauncher.exe", "python.exe"), ("venvwlauncher.exe", "pythonw.exe")):
        (launchers / name).write_text(name, encoding="utf-8")
        (root / ".venv/Scripts" / target).write_text(name, encoding="utf-8")
    for name in ("python.exe", "pythonw.exe"):
        (source / name).touch()
    path = ROOT / "scripts/localize_python.py"
    spec = importlib.util.spec_from_file_location("localize_python_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(root / "scripts/localize_python.py"))
    monkeypatch.setattr(sys, "argv", ["localize_python.py", "--source", str(source)])
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="ImportError: DLL load failed\n"
    ))
    assert module.main() == 1
    error = capsys.readouterr().err
    assert "ImportError: DLL load failed" in error
    assert "Project runtime verification failed" in error
    assert cached_pth.read_text(encoding="utf-8") == "cached original\n"
    assert editable.read_text(encoding="utf-8") == "../../../src\n"
