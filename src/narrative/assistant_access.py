"""Explicit chat configuration; task permissions are captured separately by the host."""
from typing import TYPE_CHECKING, Any

from .chat_store import ChatStore


def access_configuration(chat):
    return {"chat_id": chat["id"] if chat else None,
            "access_mode": chat["access_mode"] if chat else "standard",
            "access_revision": chat["access_revision"] if chat else 0}


def _current_chat(chat_id):
    """任务里省略会话编号时默认当前会话；编号本身是对话内部的事。"""
    if chat_id:
        return chat_id
    from .tool_execution_context import current_chat_id

    return current_chat_id()


class AssistantAccessOperationsMixin:
    if TYPE_CHECKING:
        def project_info(self) -> Any: ...

    def read_assistant_access(self, chat_id=None):
        """Read an explicitly selected chat; no chat means the current one, else the standard default."""
        chat_id = _current_chat(chat_id)
        if not chat_id:
            return access_configuration(None)
        return access_configuration(ChatStore(self.project_info().path).load(chat_id))

    def set_assistant_access(self, chat_id=None, access_mode="standard", reason="修改助手访问模式"):
        """Save the next-task mode without touching any running task or SDK session."""
        chat_id = _current_chat(chat_id)
        if not chat_id:
            raise ValueError("请先选择会话，再设置访问模式")
        store = ChatStore(self.project_info().path)
        return access_configuration(store.set_access_mode(chat_id, access_mode, reason))
