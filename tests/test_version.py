"""版本号的唯一真源：格式、dirty 分支、无 git 降级、缓存与跨入口一致性。

这里不依赖真实 git 状态（用假的 ``_git`` 构造），也不联网：任何一条都应当在
「装了 wheel、没有仓库」的机器上同样通过。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from narrative import version

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMIT = "ac8d5af1234567890abcdef1234567890abcdef"
COMMIT_DATE = "20261112"

# 锁住对外字符串的形状：dirty 只能是后缀、构建身份只能是 8 位日期 + 7 位短哈希或 unknown。
DISPLAY_FORMAT = re.compile(
    r"^v\d+\.\d+\.\d+-(?:dev|beta|rc)\.[a-z][a-z0-9]*"
    r"\+(?:\d{8}\.[0-9a-f]{7}(?:\.dirty)?|unknown)$"
)


@pytest.fixture(autouse=True)
def _reset_identity_cache():
    # 身份按进程缓存：不清掉的话，假 git 会被带到下一个用例。
    # 缓存本身由 test_identity_is_read_from_git_once_per_process 守着，这里不去要求它必须存在。
    clear = getattr(version.build_identity, "cache_clear", lambda: None)
    clear()
    yield
    clear()


def _fake_git(calls: list, *, status: str = "", commit: str = COMMIT, date: str = COMMIT_DATE):
    def fake(*args: str) -> str | None:
        calls.append(args)
        key = " ".join(args)
        if key == "rev-parse HEAD":
            return commit
        if key.startswith("log "):
            return date
        if key == "status --porcelain":
            return status
        if key == "rev-parse --abbrev-ref HEAD":
            return "feat/five-layer-and-appearance"
        raise AssertionError(f"未预期的 git 调用：{key}")

    return fake


def _install(monkeypatch, **kwargs):
    calls: list = []
    monkeypatch.setattr(version, "_git", _fake_git(calls, **kwargs))
    getattr(version.build_identity, "cache_clear", lambda: None)()
    return calls


def test_version_tuple_and_number_agree():
    assert isinstance(version.VERSION, tuple)
    assert len(version.VERSION) == 3
    assert all(isinstance(part, int) for part in version.VERSION)
    assert version.version_number() == ".".join(str(part) for part in version.VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.version_number())


def test_tag_is_number_channel_and_codename():
    assert version.tag() == f"v{version.version_number()}-{version.CHANNEL}.{version.CODENAME}"
    assert DISPLAY_FORMAT.match(f"{version.tag()}+{COMMIT_DATE}.{COMMIT[:7]}")


def test_clean_build_carries_date_and_short_hash(monkeypatch):
    _install(monkeypatch)
    identity = version.build_identity()
    assert identity.known is True
    assert identity.branch == "feat/five-layer-and-appearance"
    assert identity.dirty is False
    assert version.build_id() == f"{COMMIT_DATE}.{COMMIT[:7]}"
    assert version.display_version() == f"{version.tag()}+{COMMIT_DATE}.{COMMIT[:7]}"
    assert DISPLAY_FORMAT.match(version.display_version())
    assert version.banner() == f"{version.APP_NAME} {version.display_version()}"
    assert "未提交" not in version.banner()


def test_dirty_worktree_is_marked_in_every_string(monkeypatch):
    # 去掉 display_version 的 dirty 分支，这条就该红。
    _install(monkeypatch, status=" M src/narrative/version.py\n")
    assert version.build_identity().dirty is True
    assert version.build_id() == f"{COMMIT_DATE}.{COMMIT[:7]}.dirty"
    assert version.display_version() == f"{version.tag()}+{COMMIT_DATE}.{COMMIT[:7]}.dirty"
    assert DISPLAY_FORMAT.match(version.display_version())
    assert version.banner() == f"{version.APP_NAME} {version.display_version()}（工作区有未提交改动）"


def test_without_git_every_string_degrades_to_unknown(monkeypatch):
    # 装了 wheel / 没有 .git：降级为 unknown，且绝不抛异常。
    monkeypatch.setattr(version, "_git", lambda *args: None)
    version.build_identity.cache_clear()
    identity = version.build_identity()
    assert identity.known is False
    assert identity.commit is None
    assert version.build_id() == "unknown"
    assert version.display_version() == f"{version.tag()}+unknown"
    assert DISPLAY_FORMAT.match(version.display_version())
    assert version.banner() == f"{version.APP_NAME} {version.tag()}+unknown"
    assert "未提交" not in version.banner()


def test_git_boundary_never_raises(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(version.subprocess, "run", missing)
    assert version._git("rev-parse", "HEAD") is None

    def slow(*args, **kwargs):
        # 不额外 import subprocess：走 version 模块里已经加载的那个，省得触发重库守卫。
        raise version.subprocess.TimeoutExpired("git", version.GIT_TIMEOUT_SECONDS)

    monkeypatch.setattr(version.subprocess, "run", slow)
    assert version._git("rev-parse", "HEAD") is None

    class _Failed:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(version.subprocess, "run", lambda *args, **kwargs: _Failed())
    assert version._git("rev-parse", "HEAD") is None


def test_identity_is_read_from_git_once_per_process(monkeypatch):
    calls = _install(monkeypatch)
    version.build_identity()
    one_read = len(calls)
    assert one_read > 0
    # 三个对外字符串 + 标签全部复用同一份身份，不再调 git。
    version.build_id()
    version.display_version()
    version.banner()
    version.tag()
    version.build_identity()
    assert len(calls) == one_read
    assert version.build_identity() is version.build_identity()


def test_real_environment_string_matches_the_locked_format():
    # 不 monkeypatch：本机真实身份（干净、dirty 或 unknown 都合法）必须符合格式。
    assert DISPLAY_FORMAT.match(version.display_version())
    assert DISPLAY_FORMAT.match(version.banner().split(" ", 1)[1].split("（")[0])


def test_pyproject_version_matches_the_source():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == version.version_number()


def test_mcp_server_version_comes_from_the_same_source():
    from narrative import mcp_server

    assert mcp_server.SERVER_VERSION == version.version_number()
