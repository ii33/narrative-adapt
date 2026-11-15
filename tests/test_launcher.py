"""启动器：批处理入口之外的启动逻辑，全部是可直接断言的纯函数与注入边界。

为什么这些用例值得存在：``start-desktop.cmd`` 现在只做「找 python、透传参数、看退出码」，
真正会出事的部分（pyvenv 重指、重定向器哈希、运行时准备、低完整性硬拦、launch.log
取证）都在 :mod:`narrative.launcher` 里。以前它们在 ``.ps1`` 里，只能人工点着试；
搬进 Python 后这批用例在默认门禁里盯着，谁改坏了都能当场看见。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from narrative import launcher, runtime_security

INTEGRITY = runtime_security.MEDIUM_INTEGRITY
LOW = 0x1000


# --- 假边界 ----------------------------------------------------------------------


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """假的 ``CompletedProcess``：启动器只读 returncode/stdout/stderr 三个字段。

    刻意不导入 ``subprocess``——本模块不起任何子进程（``tests/test_slow_marking.py``
    会把默认集里导入重库的模块判成必须标慢，而这条门禁用例要留在默认集里）。
    """
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeApp:
    """被起起来的应用：只要 pid 与可等待的退出码。"""

    def __init__(self, returncode: int = 0) -> None:
        self.pid = 4242
        self.returncode = returncode
        self.waited: float | None = None

    def wait(self, timeout: float | None = None) -> int | None:
        self.waited = timeout
        return self.returncode


class FakeRunner:
    """按「解释器 + 参数」回答的假进程边界：不起子进程，只记调用。"""

    def __init__(
        self,
        *,
        base_prefix: Path = Path("C:/base/python"),
        runtime_version: str = "3.13",
        check_rc: int = 0,
        label_rc: int = 0,
        localize_rc: int = 0,
        identity_for=None,
        on_localize=None,
    ) -> None:
        self.base_prefix = base_prefix
        self.runtime_version = runtime_version
        self.check_rc = check_rc
        self.label_rc = label_rc
        self.localize_rc = localize_rc
        self.identity_for = identity_for
        self.on_localize = on_localize
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        parts = [str(part) for part in command]
        self.calls.append(parts)
        rest = parts[1:]
        if "-c" in rest:
            code = rest[rest.index("-c") + 1]
            if code == launcher.BASE_PREFIX_PROBE:
                return completed(stdout=json.dumps(str(self.base_prefix)))
            if code == launcher.VERSION_PROBE:
                return completed(stdout=self.runtime_version)
            if code == launcher.IDENTITY_PROBE:
                identity = self.identity_for(parts[0]) if self.identity_for else ["identity"]
                return completed(stdout=json.dumps(identity))
            return completed(returncode=9, stderr="unexpected probe")
        script = Path(rest[1]).name if len(rest) > 1 else ""
        if script == "check_runtime.py" and "--fix-integrity-labels" in rest:
            return completed(returncode=self.label_rc, stdout="integrity_label path=.venv level=Medium\n")
        if script == "check_runtime.py":
            return completed(returncode=self.check_rc, stdout="integrity_label summary level=Medium\n")
        if script == "localize_python.py":
            if self.on_localize is not None:
                self.on_localize(rest)
            return completed(returncode=self.localize_rc, stdout="localized\n")
        return completed(returncode=9, stderr=f"unexpected command: {parts}")

    def commands(self, script: str) -> list[list[str]]:
        return [call for call in self.calls if any(part.endswith(script) for part in call)]


def fixed_clock() -> datetime:
    return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_project(tmp_path: Path, *, runtime: bool = True, cfg_version: str = "3.13") -> Path:
    """最小工程骨架：重定向器与 venv 可执行文件字节相同，所以默认不需要重整。"""
    root = tmp_path / "project"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / "scripts").mkdir()
    for name in ("python.exe", "pythonw.exe"):
        (root / ".venv" / "Scripts" / name).write_bytes(b"redirector")
    (root / ".venv" / "pyvenv.cfg").write_text(
        f"home = C:/an/old/place\nversion_info = {cfg_version}\n", encoding="utf-8"
    )
    for script in ("check_runtime.py", "localize_python.py"):
        (root / "scripts" / script).write_text("", encoding="utf-8")
    if runtime:
        launchers = root / ".runtime" / "python" / "Lib" / "venv" / "scripts" / "nt"
        launchers.mkdir(parents=True)
        (root / ".runtime" / "python" / "Lib" / "encodings").mkdir()
        (root / ".runtime" / "python" / "python.exe").write_bytes(b"runtime")
        (launchers / "venvlauncher.exe").write_bytes(b"redirector")
        (launchers / "venvwlauncher.exe").write_bytes(b"redirector")
    return root


def build(
    root: Path,
    *,
    runner: FakeRunner,
    integrity: int | None = INTEGRITY,
    parent: launcher.ProcessSnapshot | None = None,
    environ: dict | None = None,
    spawner=None,
    qa_code: int = 0,
):
    """装一次启动器：所有边界都换成假的，只留被测逻辑。"""
    out: list[str] = []
    err: list[str] = []
    spawned: list[list[str]] = []
    app = FakeApp(qa_code)

    def spawn(paths, arguments):
        spawned.append(list(arguments))
        return app

    env = {} if environ is None else environ
    instance = launcher.Launcher(
        root=root,
        environ=env,
        run_process=runner,
        spawner=spawner or spawn,
        integrity_probe=lambda: integrity,
        snapshot_of=lambda pid: parent,
        parent_of=lambda: 0 if parent is None else parent.pid,
        command_line=lambda: "start-desktop.cmd -PrepareOnly",
        clock=fixed_clock,
        out=out.append,
        err_out=err.append,
    )
    return instance, out, err, spawned, app, env


# --- 参数 ------------------------------------------------------------------------


def test_both_spellings_parse_to_the_same_options():
    powershell = launcher.parse_options(
        ["-Project", "作品.nproj", "-QaExitAfterSeconds", "5", "-RenderLog", "qt.log",
         "-PrepareOnly", "-AllowLowIntegrity"]
    )
    posix = launcher.parse_options(
        ["--project", "作品.nproj", "--qa-exit-after-seconds", "5", "--render-log", "qt.log",
         "--prepare-only", "--allow-low-integrity"]
    )
    assert powershell == posix
    assert powershell.project == "作品.nproj"
    assert powershell.qa_exit_after_seconds == 5.0
    assert powershell.render_log == "qt.log"
    assert powershell.prepare_only and powershell.allow_low_integrity


def test_options_are_case_insensitive_and_accept_inline_values():
    options = launcher.parse_options(
        ["-PROJECT=raw/作品.nproj", "-QAEXITAFTERSECONDS=0.5", "-PrepareOnly"]
    )
    assert options.project == "raw/作品.nproj"
    assert options.qa_exit_after_seconds == 0.5
    assert options.prepare_only


@pytest.mark.parametrize(
    "argv",
    [
        ["-QaExitAfterSeconds", "soon"],
        ["-QaExitAfterSeconds", "-1"],
        ["-QaExitAfterSeconds", "nan"],
        ["-Project"],
        ["-PrepareOnly=1"],
        ["-Unknown"],
        ["stray"],
    ],
)
def test_invalid_arguments_are_usage_errors(argv):
    with pytest.raises(launcher.UsageError):
        launcher.parse_options(argv)


def test_zero_seconds_means_no_timer():
    assert launcher.parse_options(["-QaExitAfterSeconds", "0"]).qa_exit_after_seconds == 0.0


def test_help_is_recognised_without_touching_options():
    assert launcher.help_requested(["-Help"])
    assert launcher.help_requested(["--help"])
    assert not launcher.help_requested(["-PrepareOnly"])


# --- pyvenv 与解释器 --------------------------------------------------------------


def test_pyvenv_version_accepts_both_keys_and_reports_missing():
    assert launcher.pyvenv_version("version_info = 3.13\n") == (3, 13)
    assert launcher.pyvenv_version("version = 3.12.4\n") == (3, 12)
    assert launcher.pyvenv_version("home = C:/python\n") is None


def test_pyvenv_is_repointed_at_the_local_runtime_once(tmp_path):
    root = make_project(tmp_path)
    paths = launcher.project_paths(root)
    runtime = paths.runtime
    desired = launcher.repointed_pyvenv_text(paths.config.read_text(encoding="utf-8"), runtime)
    assert desired.startswith(f"home = {runtime}\n")
    assert "C:/an/old/place" not in desired
    assert "version_info = 3.13" in desired
    assert launcher.repoint_pyvenv(paths, runtime) is True
    assert paths.config.read_text(encoding="utf-8") == desired
    # 幂等：第二次不该再改文件。
    assert launcher.repoint_pyvenv(paths, runtime) is False


def test_pyvenv_redirect_lines_are_replaced_not_duplicated(tmp_path):
    text = "home = C:/old\nexecutable = C:/old/python.exe\ncommand = C:/old/python.exe -m venv\ninclude-system-site-packages = false\n"
    new_home = tmp_path / "python"
    desired = launcher.repointed_pyvenv_text(text, new_home)
    assert desired == f"home = {new_home}\ninclude-system-site-packages = false\n"


def test_mismatched_redirector_hashes_are_named(tmp_path):
    root = make_project(tmp_path)
    paths = launcher.project_paths(root)
    pairs = launcher.redirector_pairs(paths)
    assert launcher.redirectors_mismatched(pairs) == []
    (paths.runtime / "Lib" / "venv" / "scripts" / "nt" / "venvwlauncher.exe").write_bytes(b"other")
    assert launcher.redirectors_mismatched(pairs) == ["pythonw.exe"]


def test_missing_redirector_is_an_incomplete_environment(tmp_path):
    root = make_project(tmp_path)
    paths = launcher.project_paths(root)
    (paths.runtime / "Lib" / "venv" / "scripts" / "nt" / "venvlauncher.exe").unlink()
    with pytest.raises(launcher.LaunchError) as info:
        launcher.redirectors_mismatched(launcher.redirector_pairs(paths))
    assert info.value.exit_code == launcher.EXIT_VENV_INCOMPLETE
    assert "Missing CPython redirector" in str(info.value)


def test_runtime_problem_names_what_is_missing(tmp_path):
    runtime = tmp_path / ".runtime" / "python"
    assert "python.exe" in (launcher.runtime_problem(runtime) or "")
    (runtime / "python.exe").parent.mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"")
    assert "encodings" in (launcher.runtime_problem(runtime) or "")
    (runtime / "Lib" / "encodings").mkdir(parents=True)
    assert launcher.runtime_problem(runtime) is None


# --- 命令行组装 ------------------------------------------------------------------


def test_native_arguments_carry_the_documented_flags(tmp_path):
    options = launcher.parse_options(["-Project", "作品.nproj", "-QaExitAfterSeconds", "5"])
    arguments = launcher.native_app_arguments(options, tmp_path)
    assert arguments[:3] == ["-I", "-m", "narrative.native_app"]
    assert arguments[3:5] == ["--project", "作品.nproj"]
    assert arguments[5:] == ["--qa-exit-after-seconds", "5"]


def test_render_log_is_resolved_against_the_project_root(tmp_path):
    options = launcher.parse_options(["-RenderLog", "logs/qt.log"])
    arguments = launcher.native_app_arguments(options, tmp_path)
    assert arguments[-2:] == ["--render-log", str((tmp_path / "logs" / "qt.log").resolve())]


@pytest.mark.parametrize(("seconds", "text"), [(5.0, "5"), (0.5, "0.5"), (12.25, "12.25")])
def test_qa_seconds_keep_the_old_formatting(seconds, text):
    assert launcher.seconds_text(seconds) == text


def test_no_options_means_no_extra_switches():
    arguments = launcher.native_app_arguments(launcher.LaunchOptions(), Path("C:/p"))
    assert arguments == ["-I", "-m", "narrative.native_app"]


# --- launch.log ------------------------------------------------------------------


def test_launch_fields_cover_the_forensic_contract():
    parent = launcher.ProcessSnapshot(pid=77, integrity=LOW, image="C:\\Windows\\cmd.exe",
                                      command_line="cmd /c start-desktop.cmd")
    fields = launcher.launch_log_fields(
        pid=4242,
        integrity=LOW,
        parent=parent,
        allow_low_integrity=False,
        command_line='C:\\p\\.venv\\Scripts\\python.exe -I -m narrative.launcher -PrepareOnly',
        decision="blocked",
        reason="low_integrity",
    )
    assert fields["self_integrity"] == "0x1000"
    assert fields["parent_pid"] == 77
    assert fields["parent_integrity"] == "0x1000"
    assert fields["parent_image"] == "C:\\Windows\\cmd.exe"
    assert fields["allow_low_integrity"] is False
    assert fields["decision"] == "blocked"
    assert fields["reason"] == "low_integrity"
    line = launcher.format_launch_fields(fields)
    assert line.startswith("pid=4242 self_integrity=0x1000 parent_pid=77")
    # 含空格的值必须带引号，否则这一行没法按字段读回来。
    assert 'parent_cmd="cmd /c start-desktop.cmd"' in line
    assert "allow_low_integrity=False" in line


def test_unknown_integrity_and_missing_parent_are_written_as_unknown():
    fields = launcher.launch_log_fields(
        pid=1, integrity=None, parent=None, allow_low_integrity=True,
        command_line="launcher", decision="normal",
    )
    assert fields["self_integrity"] == "unknown"
    assert fields["parent_pid"] == 0
    assert fields["parent_integrity"] == "unknown"
    assert "reason" not in fields


def test_launch_log_writes_one_timestamped_line(tmp_path):
    log = launcher.LaunchLog(tmp_path / "logs" / "launch.log", clock=fixed_clock)
    log.write("pid=1 decision=normal")
    log.write("pid=1 decision=qa_exit qa_exit_code=3")
    lines = (tmp_path / "logs" / "launch.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    stamp, _, rest = lines[0].partition(" ")
    assert rest == "pid=1 decision=normal"
    assert datetime.fromisoformat(stamp) == fixed_clock(), "带时区的时间戳才能对上现场"
    assert lines[1].endswith("decision=qa_exit qa_exit_code=3")


def test_launch_log_failure_never_breaks_startup(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    launcher.LaunchLog(blocker / "launch.log").write("pid=1")  # 不抛异常就算过


# --- 判定 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "allow", "decision"),
    [
        (LOW, False, "blocked"),
        (LOW, True, "allow_low_integrity"),
        (INTEGRITY, False, "normal"),
        (0x3000, False, "normal"),
        (None, False, "normal"),
    ],
)
def test_launch_decision_table(level, allow, decision):
    assert launcher.launch_decision(level, allow=allow) == decision


def test_escape_hatch_can_come_from_the_environment():
    options = launcher.LaunchOptions()
    assert launcher.low_integrity_allowed(options, {runtime_security.ALLOW_LOW_INTEGRITY_ENV: "1"})
    assert not launcher.low_integrity_allowed(options, {})


# --- 一次启动 --------------------------------------------------------------------


def test_prepare_only_runs_the_checks_and_never_starts_the_ui(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()
    instance, out, err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-PrepareOnly"]) == launcher.EXIT_OK
    assert "Project environment is ready." in out
    assert spawned == []
    assert runner.commands("check_runtime.py"), "自检必须真的跑过"
    assert not err


def test_first_launch_prepares_the_local_runtime_from_the_base_interpreter(tmp_path):
    root = make_project(tmp_path, runtime=False)
    base = tmp_path / "base-python"

    def localize(arguments):
        # 假 localize：真的把 .runtime/python 建出来，后续检查才有东西可查。
        runtime = root / ".runtime" / "python"
        (runtime / "Lib" / "venv" / "scripts" / "nt").mkdir(parents=True)
        (runtime / "Lib" / "encodings").mkdir(parents=True)
        (runtime / "python.exe").write_bytes(b"runtime")
        (runtime / "Lib" / "venv" / "scripts" / "nt" / "venvlauncher.exe").write_bytes(b"redirector")
        (runtime / "Lib" / "venv" / "scripts" / "nt" / "venvwlauncher.exe").write_bytes(b"redirector")

    runner = FakeRunner(base_prefix=base, on_localize=localize)
    instance, out, _err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-PrepareOnly"]) == launcher.EXIT_OK
    assert any("Preparing the project-local Python runtime" in line for line in out)
    localize_call = runner.commands("localize_python.py")[0]
    assert localize_call[0] == str(base / "python.exe"), "准备运行时必须用 .venv 之外的解释器"
    assert localize_call[-2:] == ["--source", str(base)]
    assert spawned == []


def test_incomplete_existing_runtime_is_reported_and_left_alone(tmp_path):
    root = make_project(tmp_path)
    (root / ".runtime" / "python" / "Lib" / "encodings").rmdir()
    runner = FakeRunner()
    instance, _out, err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-PrepareOnly"]) == launcher.EXIT_VENV_INCOMPLETE
    assert any("Project runtime is incomplete" in line for line in err)
    assert not runner.commands("localize_python.py"), "半截运行时不覆盖、也不重建"
    assert spawned == []


def test_runtime_version_mismatch_is_refused_before_repair(tmp_path):
    root = make_project(tmp_path, cfg_version="1.0")
    runner = FakeRunner()
    instance, _out, err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-PrepareOnly"]) == launcher.EXIT_VENV_INCOMPLETE
    assert any("versions do not match" in line for line in err)
    assert not runner.commands("localize_python.py")
    assert spawned == []


def test_mismatched_redirector_triggers_one_repair(tmp_path):
    root = make_project(tmp_path)
    (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"uv launcher")
    runner = FakeRunner()
    instance, out, _err, _spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-PrepareOnly"]) == launcher.EXIT_OK
    assert any("Repairing virtual environment launchers" in line for line in out)
    localize_call = runner.commands("localize_python.py")[0]
    assert localize_call[0] == str(root / ".runtime" / "python" / "python.exe")
    assert localize_call[-2:] == ["--source", str(root / ".runtime" / "python")]


def test_redirector_repair_stops_when_the_interpreters_differ(tmp_path):
    root = make_project(tmp_path)
    (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"uv launcher")
    runner = FakeRunner(identity_for=lambda executable: [".venv" if ".venv" in executable else "runtime"])
    instance, _out, err, _spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-PrepareOnly"]) == launcher.EXIT_VENV_INCOMPLETE
    assert any("differ in Python version, ABI, or architecture" in line for line in err)
    assert not runner.commands("localize_python.py")


def test_preparation_failure_stops_startup(tmp_path):
    root = make_project(tmp_path, runtime=False)
    runner = FakeRunner(localize_rc=23)
    instance, _out, err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-PrepareOnly"]) == launcher.EXIT_RUNTIME_PREPARE_FAILED
    assert any("Project runtime preparation failed" in line for line in err)
    assert spawned == []


def test_startup_check_failure_stops_startup(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner(check_rc=17)
    instance, _out, err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run([]) == launcher.EXIT_STARTUP_CHECKS_FAILED
    assert any("Project startup checks failed" in line for line in err)
    assert spawned == []


def test_label_repair_failure_warns_but_still_starts(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner(label_rc=1)
    instance, out, _err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run([]) == launcher.EXIT_OK
    assert any("could not raise the interpreter integrity label" in line for line in out)
    assert len(spawned) == 1


def test_missing_venv_tells_the_user_to_run_setup(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    runner = FakeRunner()
    instance, _out, err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run([]) == launcher.EXIT_VENV_INCOMPLETE
    assert any("setup-environment.cmd" in line for line in err)
    assert runner.calls == [] and spawned == []


def test_unknown_options_exit_two_without_touching_the_environment(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()
    instance, _out, err, spawned, _app, _env = build(root, runner=runner)
    assert instance.run(["-Nope"]) == launcher.EXIT_USAGE
    assert any("Invalid arguments" in line for line in err)
    assert runner.calls == [] and spawned == []


def test_low_integrity_is_blocked_with_exit_code_four(tmp_path):
    root = make_project(tmp_path)
    parent = launcher.ProcessSnapshot(pid=77, integrity=LOW, image="C:\\Windows\\cmd.exe")
    runner = FakeRunner()
    instance, out, _err, spawned, _app, env = build(root, runner=runner, integrity=LOW, parent=parent)
    assert instance.run([]) == launcher.EXIT_LOW_INTEGRITY_BLOCKED
    assert any("BLOCKED" in line for line in out)
    assert any("0x1000 (Low; Medium is required)" in line for line in out)
    assert spawned == []
    assert runtime_security.ALLOW_LOW_INTEGRITY_ENV not in env
    log = (root / ".runtime" / "logs" / "launch.log").read_text(encoding="utf-8")
    assert "decision=blocked reason=low_integrity" in log
    assert "parent_pid=77" in log and "parent_integrity=0x1000" in log


def test_allow_low_integrity_starts_and_hands_the_escape_hatch_to_the_app(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()
    instance, out, _err, spawned, _app, env = build(root, runner=runner, integrity=LOW)
    assert instance.run(["-AllowLowIntegrity"]) == launcher.EXIT_OK
    assert len(spawned) == 1
    assert any("Low integrity on purpose" in line for line in out)
    # 应用侧据此挂常驻提示而不是硬失败。
    assert env[runtime_security.ALLOW_LOW_INTEGRITY_ENV] == "1"
    log = (root / ".runtime" / "logs" / "launch.log").read_text(encoding="utf-8")
    assert "decision=allow_low_integrity reason=explicit_override" in log


def test_medium_integrity_starts_normally(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()
    instance, out, _err, spawned, _app, env = build(root, runner=runner)
    assert instance.run([]) == launcher.EXIT_OK
    assert len(spawned) == 1
    assert any("Starting native desktop from" in line for line in out)
    assert runtime_security.ALLOW_LOW_INTEGRITY_ENV not in env
    log = (root / ".runtime" / "logs" / "launch.log").read_text(encoding="utf-8")
    assert "decision=normal" in log
    assert "self_integrity=0x2000" in log


def test_unreadable_integrity_is_logged_and_never_blocks(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()
    instance, _out, _err, spawned, _app, _env = build(root, runner=runner, integrity=None)
    assert instance.run([]) == launcher.EXIT_OK
    assert len(spawned) == 1
    log = (root / ".runtime" / "logs" / "launch.log").read_text(encoding="utf-8")
    assert "self_integrity=unknown" in log and "decision=normal" in log


def test_qa_mode_waits_and_returns_the_app_exit_code(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()
    instance, out, _err, spawned, app, _env = build(root, runner=runner, qa_code=3)
    assert instance.run(["-QaExitAfterSeconds", "5"]) == 3
    assert app.waited == 20.0, "等 5 秒的 QA 要留 15 秒余量"
    assert any("QA exited (code 3)" in line for line in out)
    assert spawned[0][-2:] == ["--qa-exit-after-seconds", "5"]
    log = (root / ".runtime" / "logs" / "launch.log").read_text(encoding="utf-8")
    assert "decision=qa_exit qa_exit_code=3" in log


def test_qa_timeout_is_reported(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()
    instance, _out, err, _spawned, app, _env = build(root, runner=runner)

    def timed_out(timeout=None):
        # 异常类取自启动器自己捕获的那个，测试不必导入 subprocess。
        raise launcher.subprocess.TimeoutExpired(cmd="pythonw", timeout=timeout or 0)

    app.wait = timed_out  # type: ignore[method-assign]
    assert instance.run(["-QaExitAfterSeconds", "5"]) == launcher.EXIT_QA_TIMEOUT
    assert any("Timed desktop QA did not exit" in line for line in err)


def test_spawn_failure_is_reported_with_its_own_exit_code(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner()

    def refuse(paths, arguments):
        raise OSError("no such executable")

    instance, _out, err, _spawned, _app, _env = build(root, runner=runner, spawner=refuse)
    assert instance.run([]) == launcher.EXIT_LAUNCH_FAILED
    assert any("Could not start the desktop app" in line for line in err)


def test_help_prints_usage_and_exits_zero(tmp_path):
    root = make_project(tmp_path)
    instance, out, _err, spawned, _app, _env = build(root, runner=FakeRunner())
    assert instance.run(["-Help"]) == launcher.EXIT_OK
    assert any(line.startswith("Usage: start-desktop.cmd") for line in out)
    assert spawned == []


def test_module_main_is_wired_to_the_launcher(capsys):
    """``python -m narrative.launcher`` 与 ``.cmd`` 调的是同一个入口。

    ``-Help`` 在碰环境之前就返回，所以这条用例在默认门禁里既快又无副作用。
    """
    assert launcher.main(["-Help"]) == launcher.EXIT_OK
    assert "Usage: start-desktop.cmd" in capsys.readouterr().out


def test_default_integrity_probe_needs_no_arguments():
    """默认探针就是 ``runtime_security.current_integrity_level()``：签名错了会在真机上崩。"""
    level = launcher.current_integrity()
    assert level is None or isinstance(level, int)
