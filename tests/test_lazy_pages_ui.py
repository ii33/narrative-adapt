"""页面懒创建与原文窗口化：打开工程只建当前页，长原文只渲染当前窗口。"""
from __future__ import annotations

import time
from contextlib import contextmanager


@contextmanager
def workspace_shell(tmp_path, monkeypatch, *, source_text=None):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setenv('QT_QUICK_BACKEND', 'software')
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    monkeypatch.setenv('NARRATIVE_APPEARANCE_FILE', str(tmp_path / 'appearance.json'))
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from narrative.native_app import QML_PATH
    from narrative.native_bridge import NativeBridge
    from narrative.operations import Operations

    app = QApplication.instance() or QApplication([])
    entry = tmp_path / 'lazy.nproj'
    ops = Operations.create_graph_project(entry)
    if source_text is not None:
        ops.import_source('长原文', source_text)
    bridge = NativeBridge()
    bridge.load_appearance()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('bridge', bridge)
    engine.load(QUrl.fromLocalFile(str(QML_PATH)))
    assert engine.rootObjects(), 'Main.qml 没有加载出来'
    window = engine.rootObjects()[0]
    errors: list[tuple[str, str]] = []
    bridge.errorOccurred.connect(lambda kind, message: errors.append((kind, message)))

    def settle(check=None, seconds=6.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            if check is not None and check():
                return
            time.sleep(0.01)
        if check is not None:
            raise AssertionError('界面没有等到预期状态')

    settle()

    def page(name):
        return window.findChild(object, name)

    ui = {'app': app, 'window': window, 'bridge': bridge, 'ops': ops, 'entry': entry,
          'errors': errors, 'settle': settle, 'page': page}
    bridge.openProject(str(entry))
    settle(lambda: page('browsePage') is not None)
    yield ui


def test_opening_a_project_only_creates_the_current_page(tmp_path, monkeypatch):
    with workspace_shell(tmp_path, monkeypatch) as ui:
        assert ui['page']('browsePage') is not None
        # 其余页面等第一次进入再实例化：打开工程不该把分镜、原文这些重页面全建一遍。
        for name in ('sourcePage', 'storyboardPage', 'screenplayPage', 'assetsPage',
                     'projectSettingsPage', 'staticCheckPage'):
            assert ui['page'](name) is None, name


def test_page_is_created_when_first_visited_and_kept(tmp_path, monkeypatch):
    with workspace_shell(tmp_path, monkeypatch) as ui:
        window = ui['window']
        window.setProperty('currentPage', 'project_settings')
        ui['settle'](lambda: ui['page']('projectSettingsPage') is not None)
        assert ui['page']('projectSettingsPage') is not None
        window.setProperty('currentPage', 'browse')
        ui['settle']()
        # 离开后保留：再次进入不重建，页面里的状态不会丢。
        assert ui['page']('projectSettingsPage') is not None


def test_long_source_renders_only_the_current_window(tmp_path, monkeypatch):
    from PySide6.QtCore import Q_ARG, QMetaObject

    text = ''.join(f'第{index:05d}行：甲乙丙沿河走，天开始下雨。\n' for index in range(12000))
    assert len(text) > 200_000
    with workspace_shell(tmp_path, monkeypatch, source_text=text) as ui:
        window = ui['window']
        window.setProperty('currentPage', 'source')
        ui['settle'](lambda: ui['page']('sourcePage') is not None)
        page = ui['page']('sourcePage')
        ui['settle'](lambda: (page.property('fullText') or '').startswith('第00000行'))
        assert page.property('fullText') == text
        # 只把一段交给 TextArea：2 万字以上一次性渲染会长时间占住界面线程。
        chunk = page.property('chunkSize')
        assert chunk == 40000
        assert page.property('previewText') == text[:chunk]
        assert page.property('chunkStart') == 0

        QMetaObject.invokeMethod(page, 'shiftChunk', Q_ARG('QVariant', 1))
        ui['settle'](lambda: page.property('chunkStart') == chunk)
        assert page.property('previewText') == text[chunk:chunk * 2]

        # 跳到最后一段：窗口跟着跳，选中的引文落在新窗口里。
        QMetaObject.invokeMethod(page, 'shiftChunk', Q_ARG('QVariant', 1))
        ui['settle'](lambda: page.property('chunkStart') == chunk * 2)
        preview = ui['page']('sourcePreview')
        assert preview.property('text') == page.property('previewText')
        assert not ui['errors']


def test_reveal_moves_the_window_to_the_cited_position(tmp_path, monkeypatch):
    text = ''.join(f'第{index:05d}行：甲乙丙沿河走，天开始下雨。\n' for index in range(12000))
    with workspace_shell(tmp_path, monkeypatch, source_text=text) as ui:
        window = ui['window']
        window.setProperty('currentPage', 'source')
        ui['settle'](lambda: ui['page']('sourcePage') is not None)
        page = ui['page']('sourcePage')
        ui['settle'](lambda: (page.property('fullText') or '').startswith('第00000行'))
        source_id = page.property('selectedId')
        assert source_id
        # 引用位置在 15 万字以后：不挪窗口的话，选中和滚动都落在看不见的地方。
        start = text.index('第08000行')
        window.openSourceSpan(source_id, start, start + 3)
        ui['settle'](lambda: page.property('chunkStart') > 0)
        assert page.property('chunkStart') <= start
        preview = ui['page']('sourcePreview')
        ui['settle'](lambda: preview.property('selectedText') == text[start:start + 3])
        assert preview.property('selectedText') == text[start:start + 3]
        assert not ui['errors']
