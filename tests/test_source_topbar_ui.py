"""原文页顶栏：单行布局 + 点击插入待补全指令草稿。

产品裁决（与布局改版同一批）：

* 顶栏只剩一行：``提取`` / ``复核`` 靠左，``上一段`` / 页码 / ``下一段`` 靠右；
  标题行与「任务在当前协作者会话中执行…」说明文本都已删掉。
* 点按钮不弹框、不直接发消息，只把 ``提取范围：（在此填写，例如 …）`` 这类草稿
  插进协作者输入框：已有内容换行追加，光标选中「例如 …」那一段，输入框拿到焦点
  并自动展开收起的助手面板。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication


@contextmanager
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setenv('QT_QUICK_BACKEND', 'software')
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    monkeypatch.setenv('NARRATIVE_APPEARANCE_FILE', str(tmp_path / 'appearance.json'))
    from PySide6.QtCore import QPointF, Qt, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtTest import QTest

    from narrative.native_app import QML_PATH
    from narrative.native_bridge import NativeBridge
    from narrative.operations import Operations

    ops = Operations.create_graph_project(tmp_path / 'source-topbar.nproj')
    text = ''.join(f'第{i:06d}行：飘邈之旅原文片段，李强与傅山对话。\n' for i in range(42000))
    sid = ops.import_source('飘邈之旅UTF8', text)
    app = QApplication.instance() or QApplication([])
    bridge = NativeBridge()
    results, errors = [], []
    bridge.resultReady.connect(lambda kind, payload: results.append((kind, payload)))
    bridge.errorOccurred.connect(lambda kind, message: errors.append((kind, message)))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('bridge', bridge)
    engine.load(QUrl.fromLocalFile(str(QML_PATH)))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]

    def wait(check, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if check():
                return True
            time.sleep(.01)
        return False

    def click(name):
        app.processEvents()
        QTest.qWait(30)
        button = window.findChild(object, name)
        assert button is not None, name
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint())
        app.processEvents()

    def item(name):
        return window.findChild(object, name)

    def labels_in(node):
        """收集一个子树里所有 text 属性（用来确认标题行/说明文本真的没了）。"""
        found, stack = [], [node]
        while stack:
            current = stack.pop()
            value = current.property('text')
            if isinstance(value, str):
                found.append(value)
            stack.extend(current.childItems())
        return found

    try:
        window.setWidth(1400)
        window.setHeight(860)
        bridge.openProject(str(tmp_path / 'source-topbar.nproj'))
        assert wait(lambda: any(kind == 'project' for kind, _ in results)), errors
        window.setProperty('currentPage', 'source')
        page = item('sourcePage')
        assert wait(lambda: page is not None and len(page.property('fullText')) > 0), errors
        yield SimpleNamespace(app=app, bridge=bridge, window=window, page=page, ops=ops, text=text,
                              sid=sid, results=results, errors=errors, wait=wait, click=click,
                              item=item, labels_in=labels_in, QTest=QTest, Qt=Qt,
                              QPointF=QPointF)
    finally:
        bridge.close()
        engine.deleteLater()
        app.processEvents()


def test_topbar_is_one_row_without_title_or_hint_text(tmp_path, monkeypatch):
    with shell(tmp_path, monkeypatch) as ui:
        page = ui.page
        assert ui.wait(lambda: len(page.property('previewText')) > 0)

        extract, review = ui.item('sourceSliceExtract'), ui.item('sourceSliceReview')
        prev, label, nxt = ui.item('sourceChunkPrev'), ui.item('sourceChunkLabel'), ui.item('sourceChunkNext')
        for widget in (extract, review, prev, label, nxt):
            assert widget is not None
            assert widget.property('visible')

        # 文案：不再出现「提取全文 / 复核全文」。
        assert extract.property('text') == '提取'
        assert review.property('text') == '复核'

        # 同一行：四个按钮与页码在窗口坐标里 y 相同（同一 RowLayout）。
        def mid_y(widget):
            return widget.mapToScene(ui.QPointF(0, widget.height() / 2)).y()

        rows = {round(mid_y(w)) for w in (extract, review, prev, label, nxt)}
        assert len(rows) == 1, f'顶栏没有排成一行：{sorted(rows)}'

        # 左右分布：动作按钮在左，分页导航在右。
        def left(widget):
            return widget.mapToScene(ui.QPointF(0, 0)).x()

        assert left(extract) < left(review) < left(prev) < left(label) < left(nxt)
        total = len(page.property('fullText'))
        assert label.property('text') == f'第 1–40000 字 / 共 {total} 字'

        # 不重叠：每个控件都排在前一个右边，且页码在这里能完整显示。
        def right(widget):
            return widget.mapToScene(ui.QPointF(widget.width(), 0)).x()

        widgets = (extract, review, prev, label, nxt)
        # 相邻配对：两边长度天然差一（5 个控件 vs 后 4 个），按短的走就好。
        for before, after in zip(widgets, widgets[1:], strict=False):
            assert right(before) <= left(after) + 0.5, (before.property('objectName'), after.property('objectName'))
        assert label.property('width') >= label.property('implicitWidth') - 0.5
        # 整行不越出预览卡片。
        card = extract.parentItem().parentItem().parentItem()
        assert right(nxt) <= card.mapToScene(ui.QPointF(card.width(), 0)).x()

        # 标题行与说明文本都删了：预览卡片里不再有标题、提示或说明。
        column = extract.parentItem().parentItem()
        texts = ui.labels_in(column)
        assert page.property('current')['title'] not in texts
        assert '任务在当前协作者会话中执行，核对后自动入库' not in texts
        assert '请选择文件' not in texts and '加载切片…' not in texts


def test_extract_and_review_insert_pending_draft_without_overwriting(tmp_path, monkeypatch):
    with shell(tmp_path, monkeypatch) as ui:
        assert ui.wait(lambda: len(ui.page.property('fullText')) > 0)
        window = ui.window
        window.setProperty('assistantOpen', False)
        ui.app.processEvents()

        prompt = ui.item('assistantPrompt')
        # 离屏窗口偶尔丢一次点击：点一次没效果就再点一次（草稿只会插一次，重复点会去重）。
        for _ in range(3):
            ui.click('sourceSliceExtract')
            if ui.wait(lambda: '提取范围：' in (prompt.property('text') or ''), timeout=5):
                break
        assert '提取范围：' in (prompt.property('text') or '')
        draft = prompt.property('text')
        assert draft.startswith('提取范围：（在此填写，例如 第 1–40000 字，或 第 3 段到第 5 段）')
        assert window.property('assistantOpen')  # 收起的面板被自动展开
        assert prompt.property('activeFocus')
        # 光标落在「（在此填写…）」里：选中了示例那一段，直接打字就能替换。
        start, end = prompt.property('selectionStart'), prompt.property('selectionEnd')
        assert '在此填写' in draft[:start] and '在此填写' in draft[:end]
        assert draft[start:end] == '第 1–40000 字，或 第 3 段到第 5 段'

        # 用户自己写了内容后再点「复核」：原有文字必须还在，新草稿另起一行。
        # （走面板 draft，等价于用户在输入框里自己敲了字：TextField 的 text 绑定 draft。）
        panel = ui.item('assistantPanel')
        prompt.forceActiveFocus()
        panel.setProperty('draft', '我先前写的备注')
        ui.wait(lambda: prompt.property('text') == '我先前写的备注')
        for _ in range(3):
            ui.click('sourceSliceReview')
            if ui.wait(lambda: '复核范围：' in (prompt.property('text') or ''), timeout=5):
                break
        assert '复核范围：' in (prompt.property('text') or '')
        combined = prompt.property('text')
        assert '我先前写的备注' in combined
        assert combined.index('我先前写的备注') < combined.index('复核范围：')
        assert combined.endswith('\n')
        assert not ui.errors, ui.errors
