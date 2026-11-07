"""The CLI exposes current property command names only."""
import sys

import pytest

from narrative import cli


@pytest.mark.parametrize("command", [
    "graph-property-precheck", "graph-property-state",
    "graph-property-disclosure-state", "graph-change-precheck",
    "graph-property-history", "graph-property-world-state",
])
def test_removed_property_command_names_are_rejected(monkeypatch, capsys, command):
    monkeypatch.setattr(sys, "argv", ["narrative", command])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_current_property_state_command(monkeypatch, capsys):
    class Project:
        def property_state(self, *args, **kwargs):
            assert args[:2] == ("P1", "PROP1")
            return {"value": "current"}

    monkeypatch.setattr(sys, "argv", ["narrative", "property-state", "P1", "PROP1"])
    monkeypatch.setattr(cli, "open_graph_project", lambda _: Project())
    cli.main()
    assert '"current"' in capsys.readouterr().out
