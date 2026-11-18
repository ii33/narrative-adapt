"""预演台本地服务：找到产物、起服务、能停；缺产物时明确说不可用。"""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from narrative import previs_workspace


def test_reports_unavailable_without_artifacts(tmp_path, monkeypatch):
    monkeypatch.delenv(previs_workspace.ENV_DIST, raising=False)
    monkeypatch.setattr(previs_workspace, "_candidates", lambda workspace: [tmp_path / "nope"])
    ok, reason = previs_workspace.availability(tmp_path)
    assert ok is False
    assert "未找到" in reason


def test_env_override_points_at_the_build_output(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>预演</title>", encoding="utf-8")
    monkeypatch.setenv(previs_workspace.ENV_DIST, str(dist))
    assert previs_workspace.dist_dir(tmp_path) == dist
    ok, detail = previs_workspace.availability(tmp_path)
    if not ok and "QtWebEngine" in detail:
        pytest.skip("本环境没有 QtWebEngine")
    assert ok is True


def test_server_serves_only_the_build_directory(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>预演</title>", encoding="utf-8")
    server = previs_workspace.WorkspaceServer(dist)
    try:
        url = server.start()
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
            assert "预演" in response.read().decode("utf-8")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url.replace("index.html", "missing.txt"), timeout=5)
    finally:
        server.stop()
