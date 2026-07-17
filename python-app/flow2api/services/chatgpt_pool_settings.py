"""ChatGPT worker-pool settings — Playwright multi-CDP slots + dispatch."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from flow2api.config import STORAGE_DIR

_SETTINGS_PATH = STORAGE_DIR / "chatgpt_pool_settings.json"
_LOCK = threading.Lock()

_MAX_CONCURRENT_CAP = 50
_PROFILE_MAX_CAP = 10
_PORT_MIN = 9222
_PORT_MAX = 9322


@dataclass
class PlaywrightSlot:
    id: str
    label: str = ""
    port: int = 9222

    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{int(self.port)}"

    def user_data_dir(self) -> str:
        return str(STORAGE_DIR / "playwright_slots" / self.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label or self.id,
            "port": int(self.port),
            "cdp_url": self.cdp_url(),
            "user_data_dir": self.user_data_dir(),
        }


@dataclass
class ChatgptPoolSettings:
    max_concurrent: int = 1
    profile_default_max_concurrent: int = 1
    profile_limits: dict[str, int] = field(default_factory=dict)
    profile_dispatch_disabled: list[str] = field(default_factory=list)
    playwright_slots: list[PlaywrightSlot] = field(default_factory=list)

    def normalized(self) -> ChatgptPoolSettings:
        mc = max(1, min(_MAX_CONCURRENT_CAP, int(self.max_concurrent or 1)))
        default_p = max(
            1, min(_PROFILE_MAX_CAP, int(self.profile_default_max_concurrent or 1))
        )
        limits: dict[str, int] = {}
        if isinstance(self.profile_limits, dict):
            for pid, val in self.profile_limits.items():
                if not pid or str(pid).startswith("_"):
                    continue
                limits[str(pid)] = max(1, min(_PROFILE_MAX_CAP, int(val or default_p)))
        disabled: list[str] = []
        if isinstance(self.profile_dispatch_disabled, list):
            for pid in self.profile_dispatch_disabled:
                if pid and not str(pid).startswith("_"):
                    disabled.append(str(pid))
        slots = _normalize_slots(self.playwright_slots)
        if not slots:
            slots = [PlaywrightSlot(id="pw1", label="Chrome 1", port=_PORT_MIN)]
        return ChatgptPoolSettings(
            max_concurrent=max(mc, len(slots)),
            profile_default_max_concurrent=default_p,
            profile_limits=limits,
            profile_dispatch_disabled=sorted(set(disabled)),
            playwright_slots=slots,
        )

    def to_dict(self) -> dict[str, Any]:
        n = self.normalized()
        return {
            "max_concurrent": n.max_concurrent,
            "profile_default_max_concurrent": n.profile_default_max_concurrent,
            "profile_limits": dict(n.profile_limits),
            "profile_dispatch_disabled": list(n.profile_dispatch_disabled),
            "playwright_slots": [s.to_dict() for s in n.playwright_slots],
        }


def _normalize_slots(raw: Any) -> list[PlaywrightSlot]:
    out: list[PlaywrightSlot] = []
    seen_ids: set[str] = set()
    seen_ports: set[int] = set()
    items: list[Any] = []
    if isinstance(raw, list):
        items = raw
    for item in items:
        if isinstance(item, PlaywrightSlot):
            sid = str(item.id or "").strip()
            port = int(item.port or _PORT_MIN)
            label = str(item.label or sid).strip() or sid
        elif isinstance(item, dict):
            sid = str(item.get("id") or "").strip()
            port = int(item.get("port") or _PORT_MIN)
            label = str(item.get("label") or sid).strip() or sid
        else:
            continue
        if not sid or sid.startswith("_") or sid in seen_ids:
            continue
        port = max(_PORT_MIN, min(_PORT_MAX, port))
        if port in seen_ports:
            port = _next_free_port(seen_ports)
        seen_ids.add(sid)
        seen_ports.add(port)
        out.append(PlaywrightSlot(id=sid, label=label, port=port))
    out.sort(key=lambda s: (s.port, s.id))
    return out


def _next_free_port(used: set[int]) -> int:
    for p in range(_PORT_MIN, _PORT_MAX + 1):
        if p not in used:
            return p
    return _PORT_MIN


def _next_slot_id(existing: list[PlaywrightSlot]) -> str:
    nums = []
    for s in existing:
        m = re.match(r"^pw(\d+)$", s.id, re.I)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"pw{n}"


def _load_raw() -> dict[str, Any]:
    if not _SETTINGS_PATH.is_file():
        return {}
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _settings_from_raw(raw: dict[str, Any]) -> ChatgptPoolSettings:
    return ChatgptPoolSettings(
        max_concurrent=int(raw.get("max_concurrent") or 1),
        profile_default_max_concurrent=int(
            raw.get("profile_default_max_concurrent") or 1
        ),
        profile_limits=dict(raw.get("profile_limits") or {}),
        profile_dispatch_disabled=list(raw.get("profile_dispatch_disabled") or []),
        playwright_slots=_normalize_slots(raw.get("playwright_slots") or []),
    ).normalized()


def _migrate_legacy_slot(raw: dict[str, Any]) -> dict[str, Any]:
    """If no slots yet, seed pw1 from legacy chatgpt cdp_url/port."""
    if raw.get("playwright_slots"):
        return raw
    try:
        from flow2api.services import system_ops

        cgpt = system_ops.chatgpt_config()
        cdp = str(cgpt.get("cdp_url") or "").strip()
        port = _PORT_MIN
        if cdp:
            m = re.search(r":(\d+)\s*$", cdp.rstrip("/"))
            if m:
                port = max(_PORT_MIN, min(_PORT_MAX, int(m.group(1))))
        raw = {
            **raw,
            "playwright_slots": [
                {"id": "pw1", "label": "Chrome 1", "port": port},
            ],
        }
    except Exception:
        raw = {
            **raw,
            "playwright_slots": [
                {"id": "pw1", "label": "Chrome 1", "port": _PORT_MIN},
            ],
        }
    return raw


def _load_file() -> ChatgptPoolSettings | None:
    raw = _load_raw()
    if not raw and not _SETTINGS_PATH.is_file():
        return None
    raw = _migrate_legacy_slot(raw)
    return _settings_from_raw(raw)


def _persist(data: dict[str, Any]) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_chatgpt_pool_settings() -> ChatgptPoolSettings:
    with _LOCK:
        loaded = _load_file()
        if loaded is None:
            out = ChatgptPoolSettings(
                playwright_slots=[
                    PlaywrightSlot(id="pw1", label="Chrome 1", port=_PORT_MIN)
                ]
            ).normalized()
            _persist(out.to_dict())
            return out
        raw = _load_raw()
        if not raw.get("playwright_slots"):
            _persist(loaded.to_dict())
        return loaded


def save_chatgpt_pool_settings(**fields: Any) -> ChatgptPoolSettings:
    with _LOCK:
        current = _load_file() or ChatgptPoolSettings(
            playwright_slots=[
                PlaywrightSlot(id="pw1", label="Chrome 1", port=_PORT_MIN)
            ]
        )
        data = current.to_dict()
        if "max_concurrent" in fields and fields["max_concurrent"] is not None:
            data["max_concurrent"] = int(fields["max_concurrent"])
        if (
            "profile_default_max_concurrent" in fields
            and fields["profile_default_max_concurrent"] is not None
        ):
            data["profile_default_max_concurrent"] = int(
                fields["profile_default_max_concurrent"]
            )
        if "profile_limits" in fields and isinstance(fields["profile_limits"], dict):
            merged = dict(data.get("profile_limits") or {})
            for pid, val in fields["profile_limits"].items():
                if val is None:
                    merged.pop(str(pid), None)
                else:
                    merged[str(pid)] = int(val)
            data["profile_limits"] = merged
        if "profile_dispatch_disabled" in fields:
            raw_disabled = fields["profile_dispatch_disabled"]
            if isinstance(raw_disabled, list):
                data["profile_dispatch_disabled"] = [
                    str(x)
                    for x in raw_disabled
                    if x and not str(x).startswith("_")
                ]
        if "playwright_slots" in fields and isinstance(fields["playwright_slots"], list):
            data["playwright_slots"] = fields["playwright_slots"]
        out = _settings_from_raw(data)
        _persist(out.to_dict())
        return out


def list_playwright_slots() -> list[PlaywrightSlot]:
    return list(get_chatgpt_pool_settings().playwright_slots)


def get_playwright_slot(slot_id: str) -> PlaywrightSlot | None:
    sid = str(slot_id or "").strip()
    if not sid:
        return None
    for s in list_playwright_slots():
        if s.id == sid:
            return s
    return None


def add_playwright_slot(*, label: str | None = None, port: int | None = None) -> PlaywrightSlot:
    with _LOCK:
        current = _load_file() or ChatgptPoolSettings(
            playwright_slots=[
                PlaywrightSlot(id="pw1", label="Chrome 1", port=_PORT_MIN)
            ]
        ).normalized()
        slots = list(current.playwright_slots)
        used_ports = {s.port for s in slots}
        new_port = int(port) if port else _next_free_port(used_ports)
        new_port = max(_PORT_MIN, min(_PORT_MAX, new_port))
        if new_port in used_ports:
            new_port = _next_free_port(used_ports)
        sid = _next_slot_id(slots)
        slot = PlaywrightSlot(
            id=sid,
            label=(label or f"Chrome {sid.replace('pw', '')}").strip() or sid,
            port=new_port,
        )
        slots.append(slot)
        data = current.to_dict()
        data["playwright_slots"] = [s.to_dict() for s in slots]
        data["max_concurrent"] = max(int(data.get("max_concurrent") or 1), len(slots))
        out = _settings_from_raw(data)
        _persist(out.to_dict())
        return slot


def remove_playwright_slot(slot_id: str) -> ChatgptPoolSettings:
    sid = str(slot_id or "").strip()
    if not sid:
        raise ValueError("invalid_slot_id")
    with _LOCK:
        current = _load_file() or ChatgptPoolSettings(
            playwright_slots=[
                PlaywrightSlot(id="pw1", label="Chrome 1", port=_PORT_MIN)
            ]
        ).normalized()
        slots = [s for s in current.playwright_slots if s.id != sid]
        if len(slots) == len(current.playwright_slots):
            raise KeyError("slot_not_found")
        if not slots:
            slots = [PlaywrightSlot(id="pw1", label="Chrome 1", port=_PORT_MIN)]
        data = current.to_dict()
        data["playwright_slots"] = [s.to_dict() for s in slots]
        disabled = [x for x in data.get("profile_dispatch_disabled") or [] if x != sid]
        data["profile_dispatch_disabled"] = disabled
        out = _settings_from_raw(data)
        _persist(out.to_dict())
        return out


def update_playwright_slot(
    slot_id: str,
    *,
    label: str | None = None,
    port: int | None = None,
) -> PlaywrightSlot:
    sid = str(slot_id or "").strip()
    with _LOCK:
        current = _load_file() or ChatgptPoolSettings(
            playwright_slots=[
                PlaywrightSlot(id="pw1", label="Chrome 1", port=_PORT_MIN)
            ]
        ).normalized()
        slots = list(current.playwright_slots)
        found = None
        for s in slots:
            if s.id == sid:
                found = s
                break
        if not found:
            raise KeyError("slot_not_found")
        if label is not None:
            found.label = str(label).strip() or found.id
        if port is not None:
            new_port = max(_PORT_MIN, min(_PORT_MAX, int(port)))
            used = {s.port for s in slots if s.id != sid}
            if new_port in used:
                raise ValueError("port_in_use")
            found.port = new_port
        data = current.to_dict()
        data["playwright_slots"] = [s.to_dict() for s in slots]
        out = _settings_from_raw(data)
        _persist(out.to_dict())
        for s in out.playwright_slots:
            if s.id == sid:
                return s
        raise KeyError("slot_not_found")


def profile_max_concurrent(profile_id: str) -> int:
    pid = str(profile_id or "").strip()
    if get_playwright_slot(pid) or pid == "playwright":
        return 1
    settings = get_chatgpt_pool_settings()
    if pid and pid in settings.profile_limits:
        return settings.profile_limits[pid]
    return settings.profile_default_max_concurrent


def is_chatgpt_dispatch_enabled(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    return pid not in get_chatgpt_pool_settings().profile_dispatch_disabled


def set_chatgpt_dispatch_enabled(profile_id: str, enabled: bool) -> ChatgptPoolSettings:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    current = get_chatgpt_pool_settings()
    disabled = [x for x in current.profile_dispatch_disabled if x != pid]
    if not enabled:
        disabled.append(pid)
    return save_chatgpt_pool_settings(profile_dispatch_disabled=disabled)
