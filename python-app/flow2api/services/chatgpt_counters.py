"""Persisted ChatGPT KPI counters (total / done / failed)."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from flow2api.config import STORAGE_DIR

logger = logging.getLogger(__name__)

_COUNTERS_PATH = STORAGE_DIR / "chatgpt_counters.json"
_LOCK = threading.Lock()


@dataclass
class ChatgptCounters:
    total: int = 0
    done: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"total": self.total, "done": self.done, "failed": self.failed}


def _load_unlocked() -> ChatgptCounters:
    if not _COUNTERS_PATH.is_file():
        return ChatgptCounters()
    try:
        raw = json.loads(_COUNTERS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ChatgptCounters()
        return ChatgptCounters(
            total=max(0, int(raw.get("total") or 0)),
            done=max(0, int(raw.get("done") or 0)),
            failed=max(0, int(raw.get("failed") or 0)),
        )
    except Exception as exc:
        logger.warning("read chatgpt_counters failed: %s", exc)
        return ChatgptCounters()


def _save_unlocked(counters: ChatgptCounters) -> None:
    _COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _COUNTERS_PATH.write_text(
        json.dumps(counters.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_counters() -> ChatgptCounters:
    with _LOCK:
        return _load_unlocked()


def increment_total() -> ChatgptCounters:
    with _LOCK:
        c = _load_unlocked()
        c.total += 1
        _save_unlocked(c)
        return c


def on_status_transition(old_status: str, new_status: str) -> None:
    old = str(old_status or "")
    new = str(new_status or "")
    if new == old:
        return
    with _LOCK:
        c = _load_unlocked()
        changed = False
        if new == "done" and old != "done":
            c.done += 1
            changed = True
        if new == "failed" and old != "failed":
            c.failed += 1
            changed = True
        if changed:
            _save_unlocked(c)


def reset_counters() -> ChatgptCounters:
    with _LOCK:
        c = ChatgptCounters()
        _save_unlocked(c)
        logger.info("chatgpt KPI counters reset")
        return c
