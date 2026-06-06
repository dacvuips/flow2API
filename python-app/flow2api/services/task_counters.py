"""Lifetime task KPI counters (persisted, incremented — not derived from task list)."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from flow2api.config import STORAGE_DIR

logger = logging.getLogger(__name__)

_COUNTERS_PATH = STORAGE_DIR / "task_counters.json"
_LOCK = threading.Lock()


@dataclass
class TaskCounters:
    total: int = 0
    done: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"total": self.total, "done": self.done, "failed": self.failed}


def _load_unlocked() -> TaskCounters:
    if not _COUNTERS_PATH.is_file():
        return TaskCounters()
    try:
        raw = json.loads(_COUNTERS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return TaskCounters()
        return TaskCounters(
            total=max(0, int(raw.get("total") or 0)),
            done=max(0, int(raw.get("done") or 0)),
            failed=max(0, int(raw.get("failed") or 0)),
        )
    except Exception as exc:
        logger.warning("read task_counters failed: %s", exc)
        return TaskCounters()


def _save_unlocked(counters: TaskCounters) -> None:
    _COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _COUNTERS_PATH.write_text(
        json.dumps(counters.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_counters() -> TaskCounters:
    with _LOCK:
        return _load_unlocked()


def increment_total() -> TaskCounters:
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
        if new.startswith("failed") and not old.startswith("failed"):
            c.failed += 1
            changed = True
        if changed:
            _save_unlocked(c)


def reset_counters() -> TaskCounters:
    with _LOCK:
        c = TaskCounters()
        _save_unlocked(c)
        logger.info("task KPI counters reset")
        return c


def bootstrap_from_requests_if_empty() -> None:
    """One-time backfill when counters file is still zero but DB has history."""
    with _LOCK:
        c = _load_unlocked()
        if c.total > 0:
            return
    from flow2api.db.models import RequestRecord, SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(RequestRecord).all()
        if not rows:
            return
        total = len(rows)
        done = sum(1 for r in rows if r.status == "done")
        failed = sum(1 for r in rows if str(r.status or "").startswith("failed"))
        with _LOCK:
            c = _load_unlocked()
            if c.total > 0:
                return
            c.total = total
            c.done = done
            c.failed = failed
            _save_unlocked(c)
            logger.info(
                "bootstrapped task counters total=%s done=%s failed=%s",
                total,
                done,
                failed,
            )
    finally:
        db.close()
