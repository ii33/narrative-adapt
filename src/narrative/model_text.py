"""给模型看的文字：不带本机路径、哈希和内部代号。

内部文件名、临时目录、uuid 与 sha256 都是文件系统与审计的事。它们可以留在
``.runtime``、``_extraction``、审计 JSON 和日志里，但不进模型的参数、返回值和
错误消息：模型只该看到「哪一类 + 可读编号」（``P3``、``SRC1``、``chat-3``、
``task-12``、``attachment-2``）。

本模块是模型边界的最后一道：消息先由业务代码写成可读的，再经过 ``scrub`` 兜底，
任何漏出来的绝对路径、hash 片段都会被换成类别标签，原文照旧留在审计里。
"""
from __future__ import annotations

import re

# 路径到这些字符就结束：行尾、引号、竖线，以及中文标点与括号。
_STOP = "\r\n\t\"'<>|，。；：、（）【】《》｛｝「」『』“”‘’…"
_PATH_TAIL = rf"[^{_STOP}]*"
_WINDOWS_PATH = re.compile(rf"(?:[A-Za-z]:[\\/]|\\\\[^\s\\/]+\\){_PATH_TAIL}")
_POSIX_PATH = re.compile(rf"(?<![\w:/])/(?:[^/\s{_STOP}]+/)+{_PATH_TAIL}")
# 没有盘符的内部目录（例如 ".runtime/projects/x.png"）。
_INTERNAL_PATH = re.compile(rf"(?:[A-Za-z0-9_.@-]+[\\/])*\.runtime[\\/]{_PATH_TAIL}")
# 时间戳+短哈希的运行号：20261115T120634187839Z-3c5ed8d8。
_RUN_ID = re.compile(r"\d{8}T\d{6,}[0-9]*Z(?:-[0-9a-f]{6,})?")
# 独立成词的 ≥8 位十六进制片段（uuid 截断、sha 前缀）：至少一个字母一个数字，
# 免得把 2147483647 这类普通数量当成代号。
_HEX_ID = re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{8,}(?![0-9A-Za-z])")


def _kind(path_text: str) -> str:
    """路径属于哪一类事：只报类别 + 文件名，不报目录。"""
    parts = [part for part in path_text.replace("/", "\\").split("\\") if part]
    lowered = [part.lower() for part in parts]
    name = parts[-1] if parts else ""
    if any(part == ".runtime" for part in lowered):
        return "运行时记录"
    if any(part == "chats" or part.endswith("_chats") for part in lowered):
        return "会话记录"
    if any(part in {"tasks", "turns", "assistant"} for part in lowered):
        return "任务记录"
    if any(part.endswith("_extraction") for part in lowered) or "extraction" in lowered:
        return "工程数据"
    if any(part.endswith((".sqlite3", ".db")) for part in lowered):
        return "工程数据库"
    if name.lower().endswith(".nproj"):
        return "工程入口"
    return f"本机文件 {name}" if name else "本机路径"


def _path_label(match: re.Match) -> str:
    text = match.group(0).rstrip(". ,\t")
    return f"[{_kind(text)}]"


def _hex_label(_match: re.Match) -> str:
    return "[编号]"


def scrub(text: str) -> str:
    """Replace local paths and hash-like ids: category in, internal detail out."""
    if not isinstance(text, str) or not text:
        return text
    cleaned = _WINDOWS_PATH.sub(_path_label, text)
    cleaned = _INTERNAL_PATH.sub(_path_label, cleaned)
    cleaned = _POSIX_PATH.sub(_path_label, cleaned)
    cleaned = _RUN_ID.sub("[任务编号]", cleaned)
    return _HEX_ID.sub(_hex_label, cleaned)
