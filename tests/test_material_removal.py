"""删除资料必须真的落到磁盘上；删不掉要说出来，并且下次真的会再试。

对立面是"防御性静默跳过"：`except OSError: pass` / `rmtree(ignore_errors=True)` 让人以为
删干净了，磁盘上却留着孤儿文件。这里同时锁住"真删"和"删不掉必须上报"两件事。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from agent_fixtures import TEXT, draft_result

from narrative.node_material import MaterialStore
from narrative.operations import Operations
from narrative.project_package import layout


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "materials.nproj")
    source = ops.import_source("合成", TEXT)
    assert ops.submit_extraction(source, draft_result(ended=False))["status"] == "accepted"
    node = next(row["id"] for row in ops.graph_snapshot()["nodes"] if row["node_type"] == "person")
    entry = layout(ops.path).entry
    store = MaterialStore(entry)
    return ops, store, node, entry


def _image(tmp_path, name="原件.png"):
    source = tmp_path / name
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return source


def _card_with_image(ops, node, tmp_path):
    card = ops.material_add_card(node, "带图资料")
    record = ops.material_add_image(node, card["id"], _image(tmp_path))
    path = Path(record["path"])
    assert path.is_file()
    return card, record, path


def test_removing_a_card_really_deletes_its_images(project, tmp_path):
    ops, store, node, _entry = project
    card, record, image_path = _card_with_image(ops, node, tmp_path)
    card_folder = image_path.parent

    result = ops.material_remove_card(node, card["id"])

    assert result["leftovers"] == []
    # 断言打在磁盘上：文件与资料目录都必须真的消失。
    assert not image_path.exists()
    assert not card_folder.exists()
    assert all(row["id"] != card["id"] for row in store.view(node, "", snapshot=ops.graph_snapshot())["cards"])


def test_locked_files_are_reported_and_the_report_only_lists_what_is_really_left(project, tmp_path, monkeypatch):
    ops, store, node, _entry = project
    card, _record, image_path = _card_with_image(ops, node, tmp_path)
    folder = image_path.parent
    original_unlink = Path.unlink

    def deny_folder(*_args, **_kwargs):
        raise PermissionError(13, "in use", str(folder))

    def deny_file(self, *args, **kwargs):
        if self == image_path:
            raise PermissionError(13, "in use", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("narrative.node_material.shutil.rmtree", deny_folder)
    monkeypatch.setattr(Path, "unlink", deny_file)

    result = ops.material_remove_card(node, card["id"])

    # 不抛异常（资料记录照常删掉），但磁盘上真删不掉的东西必须逐条报出来。
    assert result["removed"] == card["id"]
    assert str(image_path) in result["leftovers"] or str(folder) in result["leftovers"]
    assert image_path.exists()
    assert all(row["id"] != card["id"] for row in store.view(node, "", snapshot=ops.graph_snapshot())["cards"])


def test_report_does_not_claim_leftovers_that_the_sweep_already_cleaned(project, tmp_path, monkeypatch):
    """rmtree 被拒但文件能逐个删掉时，最终不该谎报"还有残留"。"""
    ops, _store, node, _entry = project
    card, _record, image_path = _card_with_image(ops, node, tmp_path)
    folder = image_path.parent

    def deny_folder(*_args, **_kwargs):
        raise PermissionError(13, "in use", str(folder))

    monkeypatch.setattr("narrative.node_material.shutil.rmtree", deny_folder)

    result = ops.material_remove_card(node, card["id"])

    assert result["leftovers"] == []
    assert not image_path.exists()
    assert not folder.exists()


def test_sweep_retries_and_eventually_clears_locked_files(project, tmp_path, monkeypatch):
    ops, store, node, _entry = project
    card, record, image_path = _card_with_image(ops, node, tmp_path)
    # 模拟"页面还开着这张图"：记录里已经没它了，但文件删不掉。
    data = store.load(node)
    card_row = next(row for row in data["cards"] if row["id"] == card["id"])
    card_row["images"] = []

    locked = {"on": True}
    original_unlink = Path.unlink

    def maybe_deny(self, *args, **kwargs):
        if locked["on"] and self == image_path:
            raise PermissionError(13, "in use", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", maybe_deny)

    first = store._sweep(node, data)
    assert str(image_path) in first and image_path.exists()

    locked["on"] = False  # 页面把它关掉了
    second = store._sweep(node, data)
    assert second == []
    assert not image_path.exists()


def test_sweep_still_keeps_images_that_are_referenced(project, tmp_path):
    ops, store, node, _entry = project
    _card, _record, image_path = _card_with_image(ops, node, tmp_path)
    data = store.load(node)

    assert store._sweep(node, data) == []
    assert image_path.is_file()


def test_removing_an_image_reports_a_file_it_could_not_delete(project, tmp_path, monkeypatch):
    ops, store, node, _entry = project
    card, record, image_path = _card_with_image(ops, node, tmp_path)
    original_unlink = Path.unlink

    def deny(self, *args, **kwargs):
        if self == image_path:
            raise PermissionError(13, "in use", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny)

    result = ops.material_remove_image(node, card["id"], record["id"])

    assert result["removed"] == record["id"]
    assert result["file_deleted"] is False
    assert result["leftovers"] == [str(image_path)]
    assert image_path.exists()


def test_removing_a_card_without_images_leaves_nothing_behind(project):
    ops, store, node, _entry = project
    card = ops.material_add_card(node, "无图资料")

    result = ops.material_remove_card(node, card["id"])

    assert result["leftovers"] == []
    assert not (store.root / node / card["id"]).exists()
    assert shutil is not None  # 保留导入以便与其它资料用例一致
