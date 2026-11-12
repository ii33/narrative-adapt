"""构建身份：版本号的唯一真源。

界面状态栏、启动页、CLI 与 MCP 的 ``serverInfo`` 都只从这里取版本串。改版本 =
改本模块的 ``VERSION`` / ``CHANNEL`` / ``CODENAME``，并同步 ``pyproject.toml`` 的
``version``（``tests/test_version.py`` 会拦下两者不一致）。

构建身份（提交短哈希、提交日期、是否有未提交改动、分支）运行时从 git 读，
按进程缓存一次。没有 ``.git``（例如装了 wheel）或 git 不可用时整体降级为
``unknown``：**绝不抛异常**，也绝不走网络。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_NAME = "叙事图工作台"
# 版本三元组；代号随 minor 换，历史写在 PROJECT_HANDOFF.md 的「版本号」一节。
VERSION = (0, 2, 0)
CHANNEL = "dev"
CODENAME = "islands"

UNKNOWN = "unknown"
SHORT_HASH_LENGTH = 7
GIT_TIMEOUT_SECONDS = 2.0
_REPO_ROOT = Path(__file__).resolve().parents[2]


def version_number() -> str:
    """``0.2.0``：与 ``pyproject.toml`` 的 ``version`` 必须相等。"""
    return ".".join(str(part) for part in VERSION)


def tag() -> str:
    """``v0.2.0-dev.islands``：不含构建身份的版本标签。"""
    return f"v{version_number()}-{CHANNEL}.{CODENAME}"


def _git(*args: str) -> str | None:
    """跑一条只读 git 命令；任何失败都返回 ``None``，不交互、不走网络。"""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


@dataclass(frozen=True)
class BuildIdentity:
    """一次构建的身份；``commit is None`` 表示读不到 git（无仓库 / wheel）。"""

    commit: str | None = None
    commit_date: str | None = None
    dirty: bool = False
    branch: str | None = None

    @property
    def known(self) -> bool:
        return self.commit is not None

    @property
    def build_id(self) -> str:
        """``20261112.ac8d5af``／``20261112.ac8d5af.dirty``／``unknown``。"""
        if self.commit is None:
            return UNKNOWN
        build = f"{self.commit_date}.{self.commit}"
        return f"{build}.dirty" if self.dirty else build


def _commit_and_date() -> tuple[str | None, str | None]:
    revision = _git("rev-parse", "HEAD")
    if not revision:
        return None, None
    commit_date = _git("log", "-1", "--format=%cd", "--date=format:%Y%m%d")
    if not commit_date:
        return None, None
    return revision[:SHORT_HASH_LENGTH], commit_date


@lru_cache(maxsize=1)
def build_identity() -> BuildIdentity:
    """整个进程只读一次 git，之后所有调用复用这份结果。"""
    commit, commit_date = _commit_and_date()
    if commit is None or commit_date is None:
        return BuildIdentity()
    return BuildIdentity(
        commit=commit,
        commit_date=commit_date,
        # 工作区有未提交改动 = 这个构建不能再指着某条提交说"就是它"。
        dirty=bool(_git("status", "--porcelain")),
        branch=_git("rev-parse", "--abbrev-ref", "HEAD") or None,
    )


def build_id() -> str:
    """``20261112.ac8d5af``；dirty 追加 ``.dirty``；取不到 git 时为 ``unknown``。"""
    return build_identity().build_id


def display_version() -> str:
    """``v0.2.0-dev.islands+20261112.ac8d5af``（dirty 追加 ``.dirty``）。"""
    return f"{tag()}+{build_id()}"


def banner() -> str:
    """命令行一行摘要：``叙事图工作台 v0.2.0-dev.islands+20261112.ac8d5af``。"""
    suffix = "（工作区有未提交改动）" if build_identity().dirty else ""
    return f"{APP_NAME} {display_version()}{suffix}"
