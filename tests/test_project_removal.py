"""删除工程：descriptor 说了算。

事故现场：数据目录改名成 ``renamed_store``（descriptor 里的 ``layout.database``
同步改了）之后，``removeProject(path, True)`` 只删了入口文件，整个数据目录原样留下，
用户的删除被「目录名不像 <stem>_data，可能有危险」这一串防御性守卫静默跳过。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from narrative import project_package, project_removal


def _write_entry(folder: Path, name: str, *, database: str, material: str,
                 extraction: str, chats: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / f"{name}.nproj"
    entry.write_text(json.dumps({
        "format": project_package.FORMAT,
        "version": project_package.VERSION,
        "project": {"id": name, "title": name},
        "layout": {"database": database, "material": material,
                   "extraction": extraction, "chats": chats},
    }, ensure_ascii=False), encoding="utf-8")
    return entry


def _mkdirs(base: Path, *relative: str) -> None:
    for name in relative:
        (base / name).mkdir(parents=True, exist_ok=True)


def _p(base: Path, *relative: str) -> Path:
    return base.joinpath(*relative).resolve()


def test_standard_layout_deletes_entry_and_data(tmp_path):
    folder = tmp_path / "standard"
    entry = _write_entry(folder, "normal", database="normal_data/graph.sqlite3",
                         material="normal_data/material",
                         extraction="normal_extraction", chats="normal_chats")
    _mkdirs(folder, "normal_data/material", "normal_extraction", "normal_chats")
    _p(folder, "normal_data", "graph.sqlite3").write_text("", encoding="utf-8")

    plan = project_removal.plan_removal(entry)

    # 目录 normal_data 包含 material，嵌套项不重复记账。
    assert plan.directories == [_p(folder, "normal_data"),
                                _p(folder, "normal_extraction"),
                                _p(folder, "normal_chats")]
    assert plan.retained == []
    assert plan.retained_data is False

    outcome = project_removal.apply_plan(plan)
    assert outcome.errors == []
    assert entry.exists() is False
    assert list(folder.iterdir()) == []


def test_renamed_data_folder_is_deleted_not_silently_kept(tmp_path):
    """本次 bug 的回归线：目录名不规范、material 不在 root/material，也必须真删。"""
    folder = tmp_path / "renamed"
    entry = _write_entry(folder, "renamed", database="renamed_store/graph.sqlite3",
                         material="renamed_store/renamed_material",
                         extraction="renamed_extraction", chats="renamed_chats")
    _mkdirs(folder, "renamed_store/renamed_material",
            "renamed_extraction", "renamed_chats")
    _p(folder, "renamed_store", "graph.sqlite3").write_text("", encoding="utf-8")

    plan = project_removal.plan_removal(entry)

    assert plan.directories == [_p(folder, "renamed_store"),
                                _p(folder, "renamed_extraction"),
                                _p(folder, "renamed_chats")]
    assert plan.retained == []
    outcome = project_removal.apply_plan(plan)
    assert outcome.errors == []
    assert not _p(folder, "renamed_store").exists()
    assert entry.exists() is False
    assert list(folder.iterdir()) == []


def test_shared_folder_between_two_entries_is_kept_with_reason(tmp_path):
    folder = tmp_path / "shared"
    first = _write_entry(folder, "first", database="shared_data/graph.sqlite3",
                         material="shared_data/material",
                         extraction="first_extraction", chats="first_chats")
    second = _write_entry(folder, "second", database="shared_data/graph.sqlite3",
                          material="shared_data/material",
                          extraction="second_extraction", chats="second_chats")
    _mkdirs(folder, "shared_data/material", "first_extraction", "first_chats")

    plan = project_removal.plan_removal(first, other_entries=[first, second])

    shared = _p(folder, "shared_data")
    reasons = dict(plan.retained)
    assert shared in reasons
    assert "second.nproj" in reasons[shared]
    assert "另一个工程入口" in reasons[shared]
    assert plan.directories == [_p(folder, "first_extraction"),
                                _p(folder, "first_chats")]
    assert plan.retained_data is True

    outcome = project_removal.apply_plan(plan)
    assert outcome.errors == []
    assert shared.exists(), "别人还在用的目录不许删"
    assert first.exists() is False, "入口文件总会被删"
    assert second.exists() is True


def test_folder_outside_entry_folder_is_kept_with_reason(tmp_path):
    folder = tmp_path / "inside"
    entry = _write_entry(folder, "outside", database="../elsewhere/graph.sqlite3",
                         material="../elsewhere/material",
                         extraction="outside_extraction", chats="outside_chats")
    _mkdirs(tmp_path, "elsewhere/material",
            "inside/outside_extraction", "inside/outside_chats")

    plan = project_removal.plan_removal(entry)

    elsewhere = _p(tmp_path, "elsewhere")
    reasons = dict(plan.retained)
    assert elsewhere in reasons
    assert "不在工程入口所在的目录内" in reasons[elsewhere]
    assert plan.directories == [_p(folder, "outside_extraction"),
                                _p(folder, "outside_chats")]

    outcome = project_removal.apply_plan(plan)
    assert outcome.errors == []
    assert elsewhere.exists(), "入口目录之外的东西永远不动"
    assert entry.exists() is False


def test_entry_folder_itself_is_never_deleted(tmp_path):
    folder = tmp_path / "flat"
    entry = _write_entry(folder, "flat", database="graph.sqlite3",
                         material="material", extraction="extraction", chats="chats")
    _mkdirs(folder, "material", "extraction", "chats")
    (folder / "graph.sqlite3").write_text("", encoding="utf-8")

    plan = project_removal.plan_removal(entry)

    reasons = dict(plan.retained)
    assert folder.resolve() in reasons
    assert "入口所在的目录" in reasons[folder.resolve()]
    # 嵌套在入口目录里的子目录仍照删：保留的只是目录本身那一条。
    assert plan.directories == [_p(folder, "extraction"),
                                _p(folder, "chats"),
                                _p(folder, "material")]

    outcome = project_removal.apply_plan(plan)
    assert outcome.errors == []
    assert folder.exists()
    assert entry.exists() is False
    # 入口目录被保留（理由已给），所以里面的数据库文件也跟着保留——
    # 只删一个库、留下一半的目录比留下整块更糟。子目录各自有独立判定，照删。
    assert sorted(path.name for path in folder.iterdir()) == ["graph.sqlite3"]


def test_folder_holding_another_entry_is_kept_with_reason(tmp_path):
    folder = tmp_path / "nested"
    entry = _write_entry(folder, "host", database="host_data/graph.sqlite3",
                         material="host_data/material",
                         extraction="host_extraction", chats="host_chats")
    _mkdirs(folder, "host_data/material", "host_extraction", "host_chats")
    nested = _write_entry(_p(folder, "host_data"), "nested", database="graph.sqlite3",
                          material="material", extraction="data_extraction",
                          chats="data_chats")

    plan = project_removal.plan_removal(entry, other_entries=[nested])

    data = _p(folder, "host_data")
    reasons = dict(plan.retained)
    assert data in reasons
    assert "nested.nproj" in reasons[data]
    assert plan.directories == [_p(folder, "host_extraction"),
                                _p(folder, "host_chats")]
    assert entry.exists()


def test_missing_folders_are_not_an_error(tmp_path):
    folder = tmp_path / "empty"
    entry = _write_entry(folder, "empty", database="empty_data/graph.sqlite3",
                         material="empty_data/material",
                         extraction="empty_extraction", chats="empty_chats")

    plan = project_removal.plan_removal(entry)

    assert plan.directories == []
    assert plan.retained == []
    outcome = project_removal.apply_plan(plan)
    assert outcome.errors == []
    assert outcome.removed == [entry]
    assert entry.exists() is False


def test_entry_is_unlinked_even_when_a_folder_cannot_be_removed(tmp_path, monkeypatch):
    folder = tmp_path / "busy"
    entry = _write_entry(folder, "busy", database="busy_data/graph.sqlite3",
                         material="busy_data/material",
                         extraction="busy_extraction", chats="busy_chats")
    _mkdirs(folder, "busy_data/material", "busy_extraction", "busy_chats")
    plan = project_removal.plan_removal(entry)

    def refuse(path, *args, **kwargs):
        raise OSError("目录正被占用")

    monkeypatch.setattr(project_removal.shutil, "rmtree", refuse)
    outcome = project_removal.apply_plan(plan)

    # 失败如实报出来，不当作「删掉了」；入口文件仍然是最后一步、照样 unlink。
    assert len(outcome.errors) == len(plan.directories) == 3
    assert all("正被占用" in error for error in outcome.errors)
    assert outcome.removed == [entry]
    assert entry.exists() is False
    assert all(path.exists() for path in plan.directories)


def test_plan_removal_is_pure(tmp_path):
    folder = tmp_path / "pure"
    entry = _write_entry(folder, "pure", database="pure_data/graph.sqlite3",
                         material="pure_data/material",
                         extraction="pure_extraction", chats="pure_chats")
    _mkdirs(folder, "pure_data/material", "pure_extraction", "pure_chats")

    plan = project_removal.plan_removal(entry)

    assert entry.exists()
    assert all(path.exists() for path in plan.directories)
    assert plan.retained_paths() == []


def test_non_project_entry_is_rejected(tmp_path):
    folder = tmp_path / "notanproject"
    folder.mkdir()
    stray = folder / "notes.txt"
    stray.write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        project_removal.plan_removal(stray)
    assert stray.exists()
