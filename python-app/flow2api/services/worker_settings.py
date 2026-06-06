"""Worker queue settings — persisted JSON + env defaults."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flow2api.config import STORAGE_DIR

_SETTINGS_PATH = STORAGE_DIR / "worker_settings.json"
_LOCK = threading.Lock()

_MAX_CONCURRENT_CAP = 32
_PROFILE_MAX_CAP = 8


@dataclass
class WorkerSettings:
    max_concurrent: int = 1
    task_stagger_s: float = 0.0
    profile_default_max_concurrent: int = 1
    profile_limits: dict[str, int] = field(default_factory=dict)

    def normalized(self) -> WorkerSettings:
        mc = max(1, min(_MAX_CONCURRENT_CAP, int(self.max_concurrent or 1)))
        stagger = max(0.0, min(300.0, float(self.task_stagger_s or 0.0)))
        default_p = max(1, min(_PROFILE_MAX_CAP, int(self.profile_default_max_concurrent or 1)))
        limits: dict[str, int] = {}
        if isinstance(self.profile_limits, dict):
            for pid, val in self.profile_limits.items():
                if not pid or str(pid).startswith("_"):
                    continue
                limits[str(pid)] = max(1, min(_PROFILE_MAX_CAP, int(val or default_p)))
        return WorkerSettings(
            max_concurrent=mc,
            task_stagger_s=stagger,
            profile_default_max_concurrent=default_p,
            profile_limits=limits,
        )

    def to_dict(self) -> dict[str, Any]:
        n = self.normalized()
        return {
            "max_concurrent": n.max_concurrent,
            "task_stagger_s": n.task_stagger_s,
            "profile_default_max_concurrent": n.profile_default_max_concurrent,
            "profile_limits": dict(n.profile_limits),
        }


def _defaults_from_env() -> WorkerSettings:
    import os

    mc = int(os.environ.get("FLOW2API_MAX_CONCURRENT", "1") or "1")
    stagger = float(os.environ.get("FLOW2API_TASK_STAGGER_S", "0") or "0")
    pdef = int(os.environ.get("FLOW2API_PROFILE_DEFAULT_MAX_CONCURRENT", "1") or "1")
    return WorkerSettings(
        max_concurrent=mc,
        task_stagger_s=stagger,
        profile_default_max_concurrent=pdef,
    ).normalized()


def _load_raw() -> dict[str, Any]:
    if not _SETTINGS_PATH.is_file():
        return {}
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_file() -> WorkerSettings | None:
    raw = _load_raw()
    if not raw:
        return None
    limits = raw.get("profile_limits") or {}
    if not isinstance(limits, dict):
        limits = {}
    return WorkerSettings(
        max_concurrent=int(raw.get("max_concurrent", 1)),
        task_stagger_s=float(raw.get("task_stagger_s", 0)),
        profile_default_max_concurrent=int(raw.get("profile_default_max_concurrent", 1)),
        profile_limits={str(k): int(v) for k, v in limits.items()},
    ).normalized()


def _write_settings(settings: WorkerSettings) -> WorkerSettings:
    out = settings.normalized()
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps(out.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def get_worker_settings() -> WorkerSettings:
    with _LOCK:
        saved = _load_file()
        if saved:
            return saved
        return _defaults_from_env()


def get_profile_max_concurrent(profile_id: str) -> int:
    settings = get_worker_settings()
    if profile_id in settings.profile_limits:
        return settings.profile_limits[profile_id]
    return settings.profile_default_max_concurrent


def save_worker_settings(**fields: Any) -> WorkerSettings:
    with _LOCK:
        current = _load_file() or _defaults_from_env()
        data = current.to_dict()
        if "max_concurrent" in fields and fields["max_concurrent"] is not None:
            data["max_concurrent"] = int(fields["max_concurrent"])
        if "task_stagger_s" in fields and fields["task_stagger_s"] is not None:
            data["task_stagger_s"] = float(fields["task_stagger_s"])
        if (
            "profile_default_max_concurrent" in fields
            and fields["profile_default_max_concurrent"] is not None
        ):
            data["profile_default_max_concurrent"] = int(fields["profile_default_max_concurrent"])
        if "profile_limits" in fields and isinstance(fields["profile_limits"], dict):
            merged = dict(data.get("profile_limits") or {})
            for pid, val in fields["profile_limits"].items():
                if val is None:
                    merged.pop(str(pid), None)
                else:
                    merged[str(pid)] = int(val)
            data["profile_limits"] = merged
        out = WorkerSettings(**data).normalized()
        return _write_settings(out)


def save_profile_limit(profile_id: str, max_concurrent: int) -> WorkerSettings:
    return save_worker_settings(profile_limits={str(profile_id): int(max_concurrent)})
