"""Prune plan: age windows, keep-last floors, protections, dry-run and the force threshold."""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune_artifacts.py"
DAY = 86400


@pytest.fixture(scope="module")
def prune():
    spec = importlib.util.spec_from_file_location("prune_artifacts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses 需要模块已注册才能解析注解
    spec.loader.exec_module(module)
    return module


def make_file(path: Path, *, age_days: float, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def test_old_logs_selected_and_new_kept(prune, tmp_path):
    """①超过保留天数的运行目录被选中，新目录保留。"""
    old = make_file(tmp_path / "artifacts" / "logs" / "run-old" / "a.log", age_days=30)
    fresh = make_file(tmp_path / "artifacts" / "logs" / "run-fresh" / "a.log", age_days=0)

    plan = prune.build_plan(tmp_path, days=7, keep_last=0)

    selected = {entry.path.name for entry in plan.entries}
    assert "run-old" in selected
    assert "run-fresh" not in selected
    assert old.exists() and fresh.exists()


def test_keep_last_floor_is_respected(prune, tmp_path):
    """②--keep-last 生效：比它新的份数不删，多出来的旧份才删。"""
    runs = [make_file(tmp_path / "artifacts" / "logs" / f"run-{index}" / "a.log", age_days=30 + index)
            for index in range(5)]

    plan = prune.build_plan(tmp_path, days=7, keep_last=2)

    selected = {entry.path.name for entry in plan.entries}
    assert selected == {path.parent.name for path in runs[2:]}
    assert {"run-0", "run-1"} & selected == set()


def test_protected_paths_are_never_selected(prune, tmp_path):
    """③白名单命中不删：源码、归档、密钥文件一律跳过并记录原因。"""
    for relative in ("src/narrative/a.py", "docs/archive/old/note.md", "tests/test_x.py"):
        make_file(tmp_path / relative, age_days=90)
    key = make_file(tmp_path / "artifacts" / "logs" / ".env.local", age_days=90)

    plan = prune.build_plan(tmp_path, days=7, keep_last=0)

    selected = {entry.path.as_posix() for entry in plan.entries}
    assert key.as_posix() not in selected
    reasons = {path.as_posix(): reason for path, reason in plan.skipped}
    assert reasons[key.as_posix()] == "疑似密钥文件"
    for relative in ("src/narrative/a.py", "docs/archive/old/note.md"):
        assert prune.protected_reason(tmp_path / relative, tmp_path) is not None
        assert (tmp_path / relative).exists()


def test_dry_run_lists_without_deleting(prune, tmp_path):
    """④默认 dry-run：只出清单，文件仍在。"""
    victim = make_file(tmp_path / "artifacts" / "extraction-consistency" / "run-1" / "report.md", age_days=30)

    plan = prune.build_plan(tmp_path, days=7, keep_last=0)
    report = prune.format_report(plan, mode="dry-run", days=7)

    assert victim.parent in [entry.path for entry in plan.entries]
    assert prune.exit_code(plan, force=False) == prune.EXIT_OK
    assert "dry-run" in report
    assert "artifacts/extraction-consistency" in report
    assert victim.exists()


def test_force_threshold_blocks_large_runs(prune, tmp_path, monkeypatch):
    """⑤总量超阈值且没有 --force 时返回 2，并且不删。"""
    make_file(tmp_path / "artifacts" / "logs" / "run-big" / "a.log", age_days=30, size=64)

    plan = prune.build_plan(tmp_path, days=7, keep_last=0)
    monkeypatch.setattr(prune, "SIZE_LIMIT", 16)

    assert plan.total_size > 16
    assert prune.exit_code(plan, force=False) == prune.EXIT_NEEDS_FORCE
    assert prune.exit_code(plan, force=True) == prune.EXIT_OK


def test_ledgers_prune_but_tasks_md_stays(prune, tmp_path):
    """⑥TASKS.md 不动，TASKS-<member>.md 归清理范围。"""
    tasks = make_file(tmp_path / "TASKS.md", age_days=90)
    ledger = make_file(tmp_path / "TASKS-test-conventions.md", age_days=90)

    plan = prune.build_plan(tmp_path, days=7, keep_last=0)

    selected = {entry.path.name for entry in plan.entries}
    assert "TASKS-test-conventions.md" in selected
    assert "TASKS.md" not in selected
    assert tasks.exists() and ledger.exists()


def test_report_subtotals_match_entries(prune, tmp_path):
    """⑦日志里的分类小计与逐项清单一致。"""
    make_file(tmp_path / "artifacts" / "logs" / "run-a" / "a.log", age_days=30, size=10)
    make_file(tmp_path / "artifacts" / "logs" / "run-b" / "a.log", age_days=30, size=20)
    make_file(tmp_path / "artifacts" / "probes" / "probe-a" / "a.log", age_days=30, size=40)

    plan = prune.build_plan(tmp_path, days=7, keep_last=0)
    subtotals = plan.subtotals()
    report = prune.format_report(plan, mode="dry-run", days=7)

    assert subtotals["artifacts/logs"] == (2, 30)
    assert subtotals["artifacts/probes"] == (1, 40)
    assert sum(count for count, _ in subtotals.values()) == len(plan.entries)
    assert sum(size for _, size in subtotals.values()) == plan.total_size
    assert "artifacts/logs: 2 项" in report
    assert "artifacts/probes: 1 项" in report


def test_apply_deletes_selected_and_logs(prune, tmp_path):
    """`--apply` 真删选中的项，并把清单写进 artifacts/logs/prune-<日期>.log。"""
    victim = make_file(tmp_path / "artifacts" / "logs" / "run-old" / "a.log", age_days=30)
    plan = prune.build_plan(tmp_path, days=7, keep_last=0)
    report = prune.format_report(plan, mode="apply", days=7)
    log = prune.write_log(report, root=tmp_path)

    removed = prune.apply_plan(plan)

    assert victim.parent in removed
    assert not victim.parent.exists()
    assert log.parent == tmp_path / "artifacts" / "logs"
    assert log.name.startswith("prune-")
    assert "apply" in log.read_text(encoding="utf-8")
