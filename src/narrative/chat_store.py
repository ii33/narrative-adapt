"""协作者的会话：一个工程一个目录，一次对话一个文件。

会话要留下来，因为一次对话里攒起来的东西（问过什么、查到了什么）比答案本身更值钱。
所以聊天记录落在工程旁边，不在数据库里：它不是图的一部分，也没有证据语义。

    <工程>_chats/<会话编号>.json

每份记录带一个 `session`，那是 harness 侧的会话号。重开窗口时用同一个号，
模型那边就能接上——我们存的是给人看的对话，`session` 才是给模型的记忆。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4

from .readable_ids import looks_generated, next_file_id

MAX_TITLE = 24


def _now() -> str:
    # 毫秒：一秒内建两个会话时，「最近说过」还分得出先后。
    return datetime.now(UTC).isoformat(timespec="milliseconds")


_ID_INDEX = ".chat_ids"
# 新式会话编号：工程内序数。旧工程的 uuid 文件名只在读取时补映射，不改写。
_READABLE_CHAT = re.compile(r"chat-[1-9][0-9]*\Z")


def _is_readable(identifier: str) -> bool:
    return bool(_READABLE_CHAT.fullmatch(identifier))


def _replace(temporary: Path, target: Path) -> None:
    """Windows 上杀毒/索引可能短暂锁住目标文件；有界重试原子替换。"""
    for attempt in range(5):
        try:
            temporary.replace(target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)


def _title_from(text: str) -> str:
    """第一条消息就是标题：取前一段，去掉换行。"""
    line = " ".join(str(text or "").split())
    if not line:
        return "新会话"
    return line[:MAX_TITLE] + ("…" if len(line) > MAX_TITLE else "")


_CHAT_LOCKS: dict[str, threading.RLock] = {}
_CHAT_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path):
    # Resolve the directory, not the replace target: on Windows resolving an
    # existing file itself briefly opens it before we can acquire this lock.
    key = os.path.normcase(str(path.parent.resolve() / path.name))
    with _CHAT_LOCKS_GUARD:
        return _CHAT_LOCKS.setdefault(key, threading.RLock())


def _locked_chat(method):
    @wraps(method)
    def locked(self, chat_id, *args, **kwargs):
        with _path_lock(self.path_for(chat_id)):
            return method(self, chat_id, *args, **kwargs)
    return locked


def _locked_active(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        # When nested, always acquire pointer before chat. Message mutations
        # never access the pointer while holding their chat lock.
        with _path_lock(self.root / ".active"):
            return method(self, *args, **kwargs)
    return locked


class ChatStore:
    """一个工程的会话目录。没有缓存：读一次是一次的真实内容。"""

    def __init__(self, project: Path):
        from .project_package import layout

        self.project = Path(project)
        # 包和裸工程问同一处：包里的会话在 <包>/chats。
        self.root = layout(self.project).chats

    # -- 可读会话编号 --------------------------------------------------

    def _index_path(self) -> Path:
        return self.root / _ID_INDEX

    def _read_index(self) -> dict:
        try:
            data = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        raw = data if isinstance(data, dict) else {}
        legacy = raw.get("legacy")
        legacy = legacy if isinstance(legacy, dict) else {}
        next_number = raw.get("next")
        return {
            "next": next_number if type(next_number) is int and next_number > 0 else 1,
            "legacy": {stem: readable for stem, readable in legacy.items()
                       if isinstance(stem, str) and isinstance(readable, str) and _is_readable(readable)},
        }

    def _write_index(self, index: dict) -> None:
        path = self._index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".chat-ids-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                json.dump(index, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _legacy_stems(self) -> list[str]:
        """旧工程里 uuid 命名的会话文件，按创建时间排序（同一时刻按文件名兜底）。"""
        rows: list[tuple[str, str]] = []
        for path in self.root.glob("*.json"):
            if path.name.startswith(".") or not looks_generated(path.stem):
                continue
            try:
                created = str(json.loads(path.read_text(encoding="utf-8")).get("created_at") or "")
            except (OSError, json.JSONDecodeError, AttributeError):
                created = ""
            rows.append((created, path.stem))
        return [stem for _created, stem in sorted(rows)]

    def _ensure_index(self) -> dict:
        """给还没编号的旧会话补 ``chat-N``：按出现（创建）顺序，只写索引不动历史文件。"""
        index = self._read_index()
        if not self.root.is_dir():
            return index
        # 常见情况：没有待补的旧文件，一次目录枚举即可，不读会话内容。
        unmapped = any(path.name and not path.name.startswith(".") and looks_generated(path.stem)
                       and path.stem not in index["legacy"] for path in self.root.glob("*.json"))
        if not unmapped:
            return index
        missing = [stem for stem in self._legacy_stems() if stem not in index["legacy"]]
        if not missing:
            return index
        for stem in missing:
            number = index["next"]
            while (self.root / f"chat-{number}.json").is_file() or f"chat-{number}" in index["legacy"].values():
                number += 1
            index["legacy"][stem] = f"chat-{number}"
            index["next"] = number + 1
        self._write_index(index)
        return index

    def file_stem(self, chat_id: str) -> str:
        """会话编号对应的文件名（可读编号与旧 uuid 都能认）。"""
        identifier = str(chat_id)
        for stem, readable in self._read_index()["legacy"].items():
            if readable == identifier:
                return stem
        if (self.root / f"{identifier}.json").is_file():
            return identifier
        # 还没补映射的旧工程：这时才扫一次目录，把 uuid 文件名认成 chat-N。
        for stem, readable in self._ensure_index()["legacy"].items():
            if readable == identifier or stem == identifier:
                return stem
        return identifier

    def _readable(self, stem: str) -> str:
        if _is_readable(stem):
            return stem
        for legacy, readable in self._ensure_index()["legacy"].items():
            if legacy == stem:
                return readable
        return stem

    def readable(self, chat_id: str) -> str:
        """任何形式的会话编号 → 可读编号；进模型的字段只用这一个。"""
        try:
            return self._readable(self.file_stem(chat_id))
        except ValueError:
            return str(chat_id)

    def _next_chat_id(self) -> str:
        index_path = self._index_path()
        with _path_lock(index_path):
            index = self._ensure_index()
            taken = set(index["legacy"].values()) | {path.stem for path in self.root.glob("chat-*.json")}
            identifier = next_file_id(index, "chat-", taken)
            self._write_index(index)
            return identifier

    # -- 读写 ----------------------------------------------------------

    def path_for(self, chat_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(chat_id)):
            raise ValueError("会话编号无效")
        return self.root / f"{self.file_stem(str(chat_id))}.json"

    @_locked_chat
    def load(self, chat_id: str) -> dict:
        path = self.path_for(chat_id)
        if not path.is_file():
            raise ValueError(f"会话不存在：{chat_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"会话文件损坏：{chat_id}")
        # 可读编号就是会话身份；旧文件里的 uuid 只留在磁盘上。
        data["id"] = self._readable(path.stem)
        data.setdefault("session", data["id"])
        data.setdefault("title", "新会话")
        data.setdefault("access_mode", "standard")
        data.setdefault("access_revision", 0)
        if not isinstance(data["access_mode"], str) or data["access_mode"] not in {"standard", "full"}:
            raise ValueError("会话访问模式无效")
        if type(data["access_revision"]) is not int or data["access_revision"] < 0:
            raise ValueError("会话访问设置修订无效")
        data["messages"] = data.get("messages") if isinstance(data.get("messages"), list) else []
        return data

    def _save(self, data: dict) -> dict:
        identifier = str(data["id"])
        stem = self.file_stem(identifier)
        path = self.root / f"{stem}.json"
        with _path_lock(path):
            data["id"] = self._readable(stem)
            data["updated_at"] = _now()
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {key: value for key, value in data.items() if not key.startswith("_")}
            if not _is_readable(stem):
                # 旧文件的 uuid 身份不改写；可读编号只在读取时映射。
                payload["id"] = stem
            temporary = path.with_suffix(f".{uuid4().hex}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _replace(temporary, path)
            return data

    @_locked_active
    def set_active(self, chat_id: str) -> dict:
        chat = self.load(chat_id)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / ".active"
        temporary = self.root / f".active-{uuid4().hex}.tmp"
        temporary.write_text(json.dumps({"chat_id": chat["id"]}), encoding="utf-8")
        _replace(temporary, target)
        return chat

    @_locked_active
    def active(self, *, create: bool = False) -> dict | None:
        try:
            pointer = json.loads((self.root / ".active").read_text(encoding="utf-8"))
            return self.load(str(pointer["chat_id"]))
        except (FileNotFoundError, ValueError, TypeError, KeyError):
            chat = self.latest()
            if chat is not None:
                return self.set_active(chat["id"])
            return self.create() if create else None

    def select(self, chat_id: str | None = None, *, create: bool = True,
               activate: bool = True) -> dict | None:
        if chat_id:
            return self.set_active(chat_id) if activate else self.load(chat_id)
        return self.active(create=create)

    def sdk_directory(self) -> Path:
        from .project_package import layout

        # Preserve the assistant's real SDK home and cwd, including existing
        # chat session contexts. Extraction history in conversation/ is untouched.
        return layout(self.project).extraction / "assistant"

    # -- 会话 ----------------------------------------------------------

    def create(self, title: str = "", *, template: dict | None = None) -> dict:
        # 编号就是身份：工程内序数 chat-3，文件也叫 chat-3.json。
        chat_id = self._next_chat_id()
        chat = self._save({
            "id": chat_id,
            "session": chat_id,
            "title": (title or "").strip() or "新会话",
            "created_at": _now(),
            "access_mode": "standard",
            "access_revision": 0,
            "messages": [],
            # 会话模板在创建时冻结：系统提示词全文与工具名单是快照，
            # 之后改模板不影响这个会话（工具数组不变，提示词缓存才稳）。
            **({"chat_template": template} if template else {}),
        })
        self.set_active(chat_id)
        return chat

    def list(self) -> list[dict]:
        """最近说过的排在前面；坏文件跳过，不让一个坏文件挡住整个列表。"""
        if not self.root.is_dir():
            return []
        rows: list[dict] = []
        for path in self.root.glob("*.json"):
            if path.name.startswith("."):
                continue  # .chat_ids 一类的索引文件不是会话
            try:
                with _path_lock(path):
                    data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            raw = data.get("messages")
            messages: list = raw if isinstance(raw, list) else []
            identifier = self._readable(path.stem)
            rows.append({
                "id": identifier,
                "session": str(data.get("session") or identifier),
                "title": str(data.get("title") or "新会话"),
                "updated_at": str(data.get("updated_at") or ""),
                "created_at": str(data.get("created_at") or ""),
                "message_count": len(messages),
            })
        # 时间戳相同（同一毫秒）时用 id 兜底，列表顺序才不会跟着文件枚举顺序变。
        rows.sort(key=lambda row: (row["updated_at"], row["created_at"], row["id"]), reverse=True)
        return rows

    def template(self, chat_id: str) -> dict | None:
        """会话创建时冻结的模板快照；没有模板的旧会话返回 None。"""
        try:
            frozen = self.load(chat_id).get("chat_template")
        except (OSError, json.JSONDecodeError, KeyError):
            return None
        return frozen if isinstance(frozen, dict) else None

    @_locked_chat
    def rename(self, chat_id: str, title: str) -> dict:
        data = self.load(chat_id)
        data["title"] = (title or "").strip() or data["title"]
        return self._save(data)

    @_locked_chat
    def set_access_mode(self, chat_id: str, access_mode: str, reason: str) -> dict:
        if not isinstance(access_mode, str) or access_mode not in {"standard", "full"}:
            raise ValueError("访问模式须为standard或full")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("修改访问模式须提供理由")
        data = self.load(chat_id)
        if data["access_mode"] == access_mode:
            return data
        data["access_mode"] = access_mode
        data["access_revision"] += 1
        data["access_mode_changed_at"] = _now()
        data["access_mode_reason"] = reason
        return self._save(data)

    @_locked_chat
    def delete(self, chat_id: str) -> None:
        path = self.path_for(chat_id)
        if path.is_file():
            path.unlink()

    # -- 消息 ----------------------------------------------------------

    @_locked_chat
    def append(self, chat_id: str, role: str, text: str, **extra) -> dict:
        """追加一条消息；第一条用户消息顺手把会话标题起好。"""
        data = self.load(chat_id)
        message = {"role": role, "text": str(text or ""), "at": _now()}
        message.update({key: value for key, value in extra.items() if value not in (None, "", [])})
        data["messages"].append(message)
        if role == "user" and data["title"] in {"", "新会话"}:
            data["title"] = _title_from(text)
        return self._save(data)

    @_locked_chat
    def annotate_message(self, chat_id: str, message_id: str, **extra) -> dict:
        """Attach completed task metadata to an already streamed model message."""
        data = self.load(chat_id)
        for message in data["messages"]:
            if message.get("message_id") == message_id:
                message.update(extra)
                return self._save(data)
        raise ValueError("模型消息不存在")

    @_locked_chat
    def annotate_steering(self, chat_id, message_id, status, **extra):
        ranks = {'sending': 0, 'queued': 1, 'not_delivered': 2, 'delivered': 3}
        if status not in ranks:
            raise ValueError('插话状态无效')
        data = self.load(chat_id)
        for message in data['messages']:
            if message.get('message_id') == message_id and message.get('role') == 'user':
                previous = message.get('steering_status', 'sending')
                if ranks[status] >= ranks.get(previous, 0):
                    message.update(extra, steering_status=status)
                    self._save(data)
                return message
        raise ValueError('插话用户消息不存在')

    def latest(self) -> dict | None:
        rows = self.list()
        return self.load(rows[0]["id"]) if rows else None
