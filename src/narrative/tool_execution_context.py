"""Task-local program provenance, separate from model-supplied tool arguments."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_METADATA: ContextVar[dict[str, str] | None] = ContextVar("tool_execution_metadata", default=None)
_IDENTITY: ContextVar[dict[str, str] | None] = ContextVar("tool_execution_identity", default=None)
_KEYS = {"task_id", "manifest_ref"}
@contextmanager
def bind_tool_execution_metadata(metadata: Mapping[str, str]):
    """Bind one invocation's immutable metadata and always restore its caller."""
    if set(metadata) != _KEYS or any(not isinstance(value, str) or not value.strip() for value in metadata.values()):
        raise ValueError("工具任务元数据须明确包含task_id与manifest_ref")
    token = _METADATA.set(dict(metadata))
    try:
        yield
    finally:
        _METADATA.reset(token)


def current_tool_execution_metadata() -> dict[str, str]:
    """Return a copy; a handler cannot mutate another invocation's provenance."""
    return dict(_METADATA.get() or {})


@contextmanager
def bind_tool_identity(*, chat_id: str | None = None, task_id: str | None = None):
    """Bind the current chat/task so tools can default to it instead of asking for an id."""
    identity = {key: value for key, value in (("chat_id", chat_id), ("task_id", task_id))
                if isinstance(value, str) and value.strip()}
    token = _IDENTITY.set(identity)
    try:
        yield identity
    finally:
        _IDENTITY.reset(token)


def current_chat_id() -> str | None:
    """The chat this task runs in, as a readable ``chat-N``; None outside a task."""
    return (_IDENTITY.get() or {}).get("chat_id")


def current_task_id() -> str | None:
    """The task this call belongs to, as a readable ``task-N``; None outside a task."""
    return (_IDENTITY.get() or {}).get("task_id")
