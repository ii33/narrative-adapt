"""真实 QML 界面里的深色模式：令牌、Qt 控件调色板、启动页下拉与偏好持久化。"""
from __future__ import annotations

import time
from contextlib import contextmanager


@contextmanager
def theme_shell(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setenv('QT_QUICK_BACKEND', 'software')
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    monkeypatch.setenv('NARRATIVE_APPEARANCE_FILE', str(tmp_path / 'appearance.json'))
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from narrative.native_app import QML_PATH
    from narrative.native_bridge import NativeBridge

    app = QApplication.instance() or QApplication([])
    bridge = NativeBridge()
    bridge.load_appearance()
    errors: list[tuple[str, str]] = []
    bridge.errorOccurred.connect(lambda kind, message: errors.append((kind, message)))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('bridge', bridge)
    engine.load(QUrl.fromLocalFile(str(QML_PATH)))
    assert engine.rootObjects(), 'Main.qml 没有加载出来'
    window = engine.rootObjects()[0]
    feedback = {'errors': errors, 'app': app, 'window': window, 'bridge': bridge}

    def settle(check=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            if check is None or check():
                return
            time.sleep(0.01)
        raise AssertionError('界面没有稳定下来')

    def theme_color(name):
        return window.property('theme')[name]

    def combo():
        launcher = window.findChild(object, 'launcherPage')
        return launcher.findChild(object, 'appearanceMode')

    feedback['settle'] = settle
    feedback['theme_color'] = theme_color
    feedback['combo'] = combo
    yield feedback


def test_default_is_system_and_light_tokens(tmp_path, monkeypatch):
    from narrative import theme

    with theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        assert ui['bridge'].themeMode == 'system'
        assert ui['bridge'].darkMode is False
        assert ui['theme_color']('ink') == theme.TOKENS['ink'][0]
        assert ui['theme_color']('stage') == theme.TOKENS['stage'][0]
        palette = ui['app'].palette()
        assert palette.color(palette.ColorRole.Window).name() == theme.TOKENS['paper'][0]
        assert palette.color(palette.ColorRole.Text).name() == theme.TOKENS['ink'][0]
        assert ui['combo']().property('currentIndex') == 0


def test_switching_to_dark_updates_tokens_palette_and_control(tmp_path, monkeypatch):
    from narrative import appearance, theme

    with theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        ui['bridge'].setThemeMode('dark')
        ui['settle'](lambda: ui['bridge'].darkMode)
        assert ui['theme_color']('paper') == theme.TOKENS['paper'][1]
        assert ui['theme_color']('muted') == theme.TOKENS['muted'][1]
        palette = ui['app'].palette()
        assert palette.color(palette.ColorRole.Window).name() == theme.TOKENS['paper'][1]
        assert palette.color(palette.ColorRole.Text).name() == theme.TOKENS['ink'][1]
        # 启动页的下拉框跟着模式走，才能显示用户当前的选择。
        ui['settle'](lambda: ui['combo']().property('currentIndex') == 2)
        assert appearance.load(tmp_path / 'appearance.json') == 'dark'
        assert not ui['errors']


def test_saved_dark_mode_is_restored_on_next_start(tmp_path, monkeypatch):
    from narrative import appearance, theme

    appearance.save('dark', tmp_path / 'appearance.json')
    with theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        assert ui['bridge'].themeMode == 'dark'
        assert ui['bridge'].darkMode is True
        assert ui['theme_color']('paper') == theme.TOKENS['paper'][1]
        # 切回浅色后系统偏好不再是唯一依据。
        ui['bridge'].setThemeMode('light')
        ui['settle'](lambda: not ui['bridge'].darkMode)
        assert ui['theme_color']('paper') == theme.TOKENS['paper'][0]
        assert appearance.load(tmp_path / 'appearance.json') == 'light'


def test_unknown_mode_is_reported_and_ignored(tmp_path, monkeypatch):
    from narrative import appearance

    with theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        ui['bridge'].setThemeMode('sepia')
        ui['settle'](lambda: ui['errors'])
        assert ui['bridge'].themeMode == 'system'
        assert ui['errors'][0][0] == 'appearance'
        assert appearance.load(tmp_path / 'appearance.json') == 'system'


def test_failed_write_keeps_the_switch_but_reports(tmp_path, monkeypatch):
    from narrative import appearance

    # 目录占住路径：切换要立刻生效，但保存失败必须说出来，不能装作存好了。
    blocked = tmp_path / 'appearance.json'
    blocked.mkdir()
    monkeypatch.setenv('NARRATIVE_APPEARANCE_FILE', str(blocked))
    with theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        ui['bridge'].setThemeMode('dark')
        ui['settle'](lambda: ui['errors'])
        assert ui['bridge'].darkMode is True
        assert ui['errors'][0][0] == 'appearance'
        assert appearance.load(blocked) == 'system'


class _FakeHints:
    """像真实 Qt 那样：设了具体一档就回读它，Unknown 表示继续跟系统走。"""

    def __init__(self, scheme):
        self.system_scheme = scheme
        self.forced = None
        self.calls = []

    def colorScheme(self):
        return self.system_scheme if self.forced is None else self.forced

    def setColorScheme(self, scheme):
        from PySide6.QtCore import Qt

        self.calls.append(scheme)
        self.forced = None if scheme == Qt.ColorScheme.Unknown else scheme


class _FakeApp:
    def __init__(self, scheme):
        from PySide6.QtGui import QPalette

        self._palette = QPalette()
        self.hints = _FakeHints(scheme)

    def palette(self):
        return self._palette

    def setPalette(self, palette):
        self._palette = palette

    def styleHints(self):
        return self.hints


def test_system_dark_reads_the_platform_scheme():
    from PySide6.QtCore import Qt

    from narrative import theme

    assert theme.system_dark(_FakeApp(Qt.ColorScheme.Dark)) is True
    assert theme.system_dark(_FakeApp(Qt.ColorScheme.Light)) is False
    # 平台不报告时按浅色处理，不能凭空变深色。
    assert theme.system_dark(_FakeApp(Qt.ColorScheme.Unknown)) is False


def test_explicit_mode_pins_the_window_frame():
    from PySide6.QtCore import Qt

    from narrative import theme

    app = _FakeApp(Qt.ColorScheme.Dark)
    theme.apply_palette(app, dark=False)
    assert app.hints.calls == [Qt.ColorScheme.Light]
    theme.apply_palette(app, dark=True)
    assert app.hints.calls[-1] == Qt.ColorScheme.Dark


def test_follow_system_mode_never_pins_a_scheme():
    """跟随系统时只能交回 Unknown：钉死一档会把系统偏好盖住，之后就永远读到它自己设的值。"""
    from PySide6.QtCore import Qt

    from narrative import theme

    app = _FakeApp(Qt.ColorScheme.Dark)
    theme.apply_palette(app, dark=True, follow_system=True)
    assert app.hints.calls == [Qt.ColorScheme.Unknown]
    # 交回 Unknown 之后仍能读到系统真正的深色，这是「跟随系统」继续生效的前提。
    assert theme.system_dark(app) is True
    # 控件颜色仍按解析出的深色上色，不受窗口框架策略影响。
    palette = app.palette()
    assert palette.color(palette.ColorRole.Window).name() == theme.TOKENS['paper'][1]


def test_system_mode_uses_the_os_preference(tmp_path, monkeypatch):
    """系统是深色时，「跟随系统」必须给出深色——不能因为之前画过浅色就钉死。"""
    from narrative import theme

    monkeypatch.setattr(theme, 'system_dark', lambda app=None: True)
    with theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        assert ui['bridge'].themeMode == 'system'
        assert ui['bridge'].darkMode is True
        assert ui['theme_color']('paper') == theme.TOKENS['paper'][1]
        palette = ui['app'].palette()
        assert palette.color(palette.ColorRole.Window).name() == theme.TOKENS['paper'][1]


def test_returning_to_system_drops_the_forced_scheme(tmp_path, monkeypatch):
    """先显式选深色、再切回跟随系统：必须先复位，否则读到的是自己钉进去的深色。"""
    from narrative import appearance

    with theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        ui['bridge'].setThemeMode('dark')
        ui['settle'](lambda: ui['bridge'].darkMode)
        ui['bridge'].setThemeMode('system')
        ui['settle'](lambda: ui['bridge'].themeMode == 'system')
        # 离屏平台的系统偏好是 Unknown（按浅色处理）；若没复位，仍会停在深色。
        assert ui['bridge'].darkMode is False
        assert appearance.load(tmp_path / 'appearance.json') == 'system'
