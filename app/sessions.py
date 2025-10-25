from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SessionMeta:
    id: str
    title: str
    created_at: int
    updated_at: int
    model: str | None = None


class SessionStore:
    """Lightweight JSON-backed session store for chat history.

    Not intended for heavy concurrent writes; suitable for local/dev.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"sessions": {}}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    self._data = {"sessions": {}}
            else:
                self._data = {"sessions": {}}

    def _save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def list(self) -> List[SessionMeta]:
        with self._lock:
            sessions = self._data.get("sessions", {})
            metas: List[SessionMeta] = []
            for sid, s in sessions.items():
                metas.append(SessionMeta(
                    id=sid,
                    title=s.get("title") or "",
                    created_at=int(s.get("created_at") or 0),
                    updated_at=int(s.get("updated_at") or 0),
                    model=s.get("model"),
                ))
            return metas

    def create(self, chat_id: str, *, title: str, created_at: int, model: str | None = None) -> None:
        with self._lock:
            sessions = self._data.setdefault("sessions", {})
            if chat_id not in sessions:
                sessions[chat_id] = {
                    "title": title or "",
                    "created_at": int(created_at),
                    "updated_at": int(created_at),
                    "messages": [],
                    "model": model,
                }
                self._save()

    def get(self, chat_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._data.get("sessions", {}).get(chat_id)

    def get_messages(self, chat_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            sess = self._data.get("sessions", {}).get(chat_id)
            if not sess:
                return []
            # Return a shallow copy
            return list(sess.get("messages", []))

    def append(self, chat_id: str, message: Dict[str, Any], *, updated_at: int) -> None:
        with self._lock:
            sess = self._data.get("sessions", {}).setdefault(chat_id, {
                "title": "",
                "created_at": int(updated_at),
                "updated_at": int(updated_at),
                "messages": [],
                "model": None,
            })
            sess.setdefault("messages", []).append(message)
            sess["updated_at"] = int(updated_at)
            self._save()

    def rename(self, chat_id: str, title: str, *, updated_at: int) -> bool:
        with self._lock:
            sess = self._data.get("sessions", {}).get(chat_id)
            if not sess:
                return False
            sess["title"] = title or ""
            sess["updated_at"] = int(updated_at)
            self._save()
            return True

    def update_model(self, chat_id: str, model: str, *, updated_at: int) -> bool:
        with self._lock:
            sess = self._data.get("sessions", {}).get(chat_id)
            if not sess:
                return False
            sess["model"] = model
            sess["updated_at"] = int(updated_at)
            self._save()
            return True

    def delete(self, chat_id: str) -> bool:
        with self._lock:
            sessions = self._data.get("sessions", {})
            if chat_id in sessions:
                del sessions[chat_id]
                self._save()
                return True
            return False
