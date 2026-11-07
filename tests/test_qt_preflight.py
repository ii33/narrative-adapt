import pytest

from narrative import qt_preflight


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        (ModuleNotFoundError("No module named 'PySide6'", name="PySide6"), "uv sync"),
        (ImportError("DLL load failed: missing runtime"), "DLL load failed: missing runtime"),
    ],
)
def test_qt_preflight_reports_actual_import_failure(monkeypatch, capsys, failure, diagnostic):
    def fail(_name):
        raise failure

    monkeypatch.setattr(qt_preflight.importlib, "import_module", fail)
    with pytest.raises(SystemExit) as exc:
        qt_preflight.check_qt_imports()
    assert exc.value.code == 2
    assert diagnostic in capsys.readouterr().err

def test_qt_preflight_does_not_require_widgets(monkeypatch):
    imported = []

    def import_quick_module(name):
        if name == "PySide6.QtWidgets":
            raise ImportError("Qt Widgets is unavailable")
        imported.append(name)

    monkeypatch.setattr(qt_preflight.importlib, "import_module", import_quick_module)
    qt_preflight.check_qt_imports()
    assert "PySide6.QtQuick" in imported
    assert "PySide6.QtQuickControls2" in imported
