"""最近工程列表写失败不许阻断动作，低完整性必须在界面里说出来。

事故现场：点「移出工程」→ 状态栏 `无法保存最近工程列表：attempt to write a
readonly database` → 动作整段放弃（工程没关、条目没消失）。真因是进程低完整性
（令牌继承）写不了 ``%USERPROFILE%\\.narrative``，但产品不该因此把用户的操作丢掉：
最近工程列表只是「下次启动的便利记录」，不是业务数据。
"""

import json
import sqlite3
import time

import pytest

from narrative.provider_store import GLOBAL_SCOPE

READONLY_ERROR = "attempt to write a readonly database"


class ReadonlyStore:
    """假的 ProviderStore：读得到旧值，一写就报 SQLite 的只读错误。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.put_attempts = 0

    def get(self, scope, key):
        return self.values.get((scope, key))

    def put(self, scope, key, value):
        self.put_attempts += 1
        raise sqlite3.OperationalError(READONLY_ERROR)


def _qt(app_module=None):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _wait(app, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _bridge(monkeypatch, tmp_path, store):
    from narrative import native_bridge

    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    monkeypatch.setattr(native_bridge, "ProviderStore", lambda *args, **kwargs: store)
    bridge = native_bridge.NativeBridge()
    results, errors, notices = [], [], []

    def on_result(kind, payload):
        results.append((kind, payload))

    bridge.resultReady.connect(on_result)
    bridge.errorOccurred.connect(lambda kind, message: errors.append((kind, message)))
    bridge.noticeRaised.connect(notices.append)
    return bridge, results, errors, notices


def _listed_paths(results):
    for kind, payload in reversed(results):
        if kind == "recent_projects":
            return [item["path"] for item in payload["items"]]
    return None


def test_remove_project_survives_recent_list_write_failure(tmp_path, monkeypatch):
    """清单写失败照旧：工程关掉、条目从列表消失、只发一条能看懂的提示。"""
    app = _qt()

    entry = tmp_path / "示例工程.nproj"
    entry.write_text("", encoding="utf-8")
    other = tmp_path / "另一个工程.nproj"
    other.write_text("", encoding="utf-8")
    stored = json.dumps([str(entry), str(other)], ensure_ascii=False)
    store = ReadonlyStore({(GLOBAL_SCOPE, "recent_projects"): stored})
    bridge, results, errors, notices = _bridge(monkeypatch, tmp_path, store)
    try:
        bridge._project_path = entry
        bridge.removeProject(str(entry), False)
        assert _wait(app, lambda: _listed_paths(results) is not None)

        # 动作照做：工程真的关了，条目真的从列表里消失。
        assert bridge._project_path is None
        assert any(kind == "project_removed" for kind, _payload in results)
        keys = [bridge.recentProjectKey(path) for path in _listed_paths(results)]
        assert bridge.recentProjectKey(str(entry)) not in keys
        assert bridge.recentProjectKey(str(other)) in keys
        # 清单确实没写进去：假存储里的旧值原样，说明「消失」是内存里做到的。
        assert store.values[(GLOBAL_SCOPE, "recent_projects")] == stored
        assert store.put_attempts == 1

        # 只发一条提示，而且不许当成致命错误。
        assert errors == []
        assert len(notices) == 1
        notice = notices[0]
        assert "已移出工程" in notice
        assert "最近工程" in notice and READONLY_ERROR in notice
        assert "start-desktop.cmd" in notice  # 「怎么办」也在里面
        assert not any(kind == "project" for kind, _payload in results if kind == "project")
    finally:
        bridge.close()


def test_open_project_survives_recent_list_write_failure(tmp_path, monkeypatch):
    """_remember_project 失败只提示，工程照常打开。"""
    app = _qt()
    from narrative.operations import Operations

    entry = tmp_path / "记得住的工程.nproj"
    Operations.create_graph_project(entry)
    store = ReadonlyStore()
    bridge, results, errors, notices = _bridge(monkeypatch, tmp_path, store)
    try:
        assert bridge._recent_paths() == []
        bridge.openProject(str(entry))
        assert _wait(app, lambda: any(kind == "project" for kind, _p in results) or errors)
        assert errors == []
        assert bridge._project_path is not None and bridge._project_path.name == entry.name
        opened = [payload for kind, payload in results if kind == "project"]
        assert opened and opened[0]["project"]["path"] == str(entry)
        assert len(notices) == 1
        assert "没能写进最近工程列表" in notices[0]
        assert READONLY_ERROR in notices[0]
        assert store.put_attempts == 1
    finally:
        bridge.close()


@pytest.mark.parametrize(("level", "shown"), [
    (0x1000, True),
    (0x2000, False),
    (None, False),
])
def test_bridge_shows_persistent_low_integrity_warning(monkeypatch, level, shown):
    """界面常驻提示只由真实完整性级别决定；正常进程上必须是空串（不占宽度）。"""
    _qt()
    from narrative import native_bridge, runtime_security

    monkeypatch.setattr(runtime_security, "current_integrity_level", lambda: level)
    bridge = native_bridge.NativeBridge()
    try:
        warning = bridge.integrityWarning
        assert (runtime_security.LOW_INTEGRITY_NOTICE in warning) is shown
        if shown:
            assert "改动不会写进用户目录" in warning
    finally:
        bridge.close()


def test_native_app_refuses_low_integrity_before_entering_the_ui(tmp_path, monkeypatch, capsys):
    """直接跑 pythonw（绕过脚本）时也要硬失败：写渲染日志 + 非 0 退出码。"""
    _qt()
    from narrative import native_app, quiet_children, runtime_security

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv(runtime_security.ALLOW_LOW_INTEGRITY_ENV, raising=False)
    monkeypatch.setattr(runtime_security, "current_integrity_level", lambda: 0x1000)
    monkeypatch.setattr(quiet_children, "hide_consoles", lambda: None)
    log_path = tmp_path / "render.log"
    assert native_app.main(["--render-log", str(log_path)]) == 3
    assert "低完整性" in capsys.readouterr().err
    log = log_path.read_text(encoding="utf-8")
    assert "integrity_level=0x1000" in log
    assert "startup_refused=low_integrity" in log
    assert "start-desktop.cmd" in log
