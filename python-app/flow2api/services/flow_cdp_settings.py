"""Flow CDP slots — dedicated Chrome profiles for Flow gen / Captcha Center (parallel to extension)."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from flow2api.config import STORAGE_DIR

_SETTINGS_PATH = STORAGE_DIR / "flow_cdp_settings.json"
_LOCK = threading.Lock()

_PORT_MIN = 9422
_PORT_MAX = 9522

FlowCdpRole = Literal["bridge", "center"]


@dataclass
class FlowCdpSlot:
    id: str
    label: str = ""
    port: int = _PORT_MIN
    role: str = "bridge"  # bridge = gen Image/Video · center = Recaptcha Center
    email: str = ""
    linked_profile_id: str = ""  # FlowProfile id after cookie sync (default = slot id)

    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{int(self.port)}"

    def user_data_dir(self) -> str:
        return str(STORAGE_DIR / "flow_cdp_slots" / self.id)

    def profile_id(self) -> str:
        return str(self.linked_profile_id or self.id).strip() or self.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label or self.id,
            "port": int(self.port),
            "role": self.role if self.role in ("bridge", "center") else "bridge",
            "email": self.email or "",
            "linked_profile_id": self.profile_id(),
            "cdp_url": self.cdp_url(),
            "user_data_dir": self.user_data_dir(),
        }


@dataclass
class FlowCdpSettings:
    slots: list[FlowCdpSlot] = field(default_factory=list)
    # Ports / slot ids đã xóa — không tái sử dụng khi thêm CDP mới
    retired_ports: list[int] = field(default_factory=list)
    retired_slot_ids: list[str] = field(default_factory=list)

    def normalized(self) -> FlowCdpSettings:
        return FlowCdpSettings(
            slots=_normalize_slots(self.slots),
            retired_ports=_normalize_retired_ports(self.retired_ports),
            retired_slot_ids=_normalize_retired_ids(self.retired_slot_ids),
        )

    def to_dict(self) -> dict[str, Any]:
        n = self.normalized()
        return {
            "slots": [s.to_dict() for s in n.slots],
            "retired_ports": list(n.retired_ports),
            "retired_slot_ids": list(n.retired_slot_ids),
        }


def _normalize_retired_ports(raw: Any) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            p = int(item)
        except (TypeError, ValueError):
            continue
        if p < _PORT_MIN or p > _PORT_MAX or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return sorted(out)


def _normalize_retired_ids(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        sid = str(item or "").strip()
        if not sid or sid.startswith("_") or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return sorted(out)


def _normalize_slots(raw: Any) -> list[FlowCdpSlot]:
    out: list[FlowCdpSlot] = []
    seen_ids: set[str] = set()
    seen_ports: set[int] = set()
    items: list[Any] = list(raw) if isinstance(raw, list) else []
    for item in items:
        if isinstance(item, FlowCdpSlot):
            sid = str(item.id or "").strip()
            port = int(item.port or _PORT_MIN)
            label = str(item.label or sid).strip() or sid
            role = str(item.role or "bridge").strip().lower()
            email = str(item.email or "").strip()
            linked = str(item.linked_profile_id or sid).strip() or sid
        elif isinstance(item, dict):
            sid = str(item.get("id") or "").strip()
            port = int(item.get("port") or _PORT_MIN)
            label = str(item.get("label") or sid).strip() or sid
            role = str(item.get("role") or "bridge").strip().lower()
            email = str(item.get("email") or "").strip()
            linked = str(item.get("linked_profile_id") or sid).strip() or sid
        else:
            continue
        if not sid or sid.startswith("_") or sid in seen_ids:
            continue
        if role not in ("bridge", "center"):
            role = "bridge"
        port = max(_PORT_MIN, min(_PORT_MAX, port))
        if port in seen_ports:
            port = _next_free_port(seen_ports, set())
        seen_ids.add(sid)
        seen_ports.add(port)
        out.append(
            FlowCdpSlot(
                id=sid,
                label=label,
                port=port,
                role=role,
                email=email,
                linked_profile_id=linked,
            )
        )
    out.sort(key=lambda s: (0 if s.role == "bridge" else 1, s.port, s.id))
    return out


def _next_free_port(used: set[int], retired: set[int]) -> int:
    """Cấp port chưa dùng và chưa từng bị xóa (retired)."""
    blocked = used | retired
    for p in range(_PORT_MIN, _PORT_MAX + 1):
        if p not in blocked:
            return p
    raise ValueError("no_free_cdp_port")


def _next_slot_id(
    existing: list[FlowCdpSlot],
    role: str = "bridge",
    *,
    retired_ids: set[str] | None = None,
) -> str:
    prefix = "flow" if role != "center" else "center"
    nums: list[int] = []
    for s in existing:
        m = re.match(rf"^{prefix}(\d+)$", s.id, re.I)
        if m:
            nums.append(int(m.group(1)))
    for sid in retired_ids or set():
        m = re.match(rf"^{prefix}(\d+)$", sid, re.I)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}{n}"


def _load_raw() -> dict[str, Any]:
    if not _SETTINGS_PATH.is_file():
        return {}
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _settings_from_raw(raw: dict[str, Any]) -> FlowCdpSettings:
    return FlowCdpSettings(
        slots=_normalize_slots(raw.get("slots") or []),
        retired_ports=_normalize_retired_ports(raw.get("retired_ports") or []),
        retired_slot_ids=_normalize_retired_ids(raw.get("retired_slot_ids") or []),
    ).normalized()


def _persist(data: dict[str, Any]) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_flow_cdp_settings() -> FlowCdpSettings:
    with _LOCK:
        raw = _load_raw()
        if not raw and not _SETTINGS_PATH.is_file():
            out = FlowCdpSettings(slots=[]).normalized()
            _persist(out.to_dict())
            return out
        return _settings_from_raw(raw)


def list_flow_cdp_slots() -> list[FlowCdpSlot]:
    return list(get_flow_cdp_settings().slots)


def get_flow_cdp_slot(slot_id: str) -> FlowCdpSlot | None:
    sid = str(slot_id or "").strip()
    if not sid:
        return None
    for s in list_flow_cdp_slots():
        if s.id == sid:
            return s
    return None


def add_flow_cdp_slot(
    *,
    label: str | None = None,
    port: int | None = None,
    role: str = "bridge",
) -> FlowCdpSlot:
    role_n = str(role or "bridge").strip().lower()
    if role_n not in ("bridge", "center"):
        role_n = "bridge"
    with _LOCK:
        current = _settings_from_raw(_load_raw())
        slots = list(current.slots)
        retired_ports = set(current.retired_ports)
        retired_ids = set(current.retired_slot_ids)
        used_ports = {s.port for s in slots}
        if port is not None:
            new_port = max(_PORT_MIN, min(_PORT_MAX, int(port)))
            if new_port in used_ports or new_port in retired_ports:
                new_port = _next_free_port(used_ports, retired_ports)
        else:
            new_port = _next_free_port(used_ports, retired_ports)
        sid = _next_slot_id(slots, role_n, retired_ids=retired_ids)
        # Slot id vừa cấp không còn trong retired
        retired_ids.discard(sid)
        n = re.search(r"(\d+)$", sid)
        default_label = (
            f"Flow CDP {n.group(1)}" if role_n == "bridge" else f"Captcha Center CDP {n.group(1)}"
        )
        slot = FlowCdpSlot(
            id=sid,
            label=(label or default_label).strip() or sid,
            port=new_port,
            role=role_n,
            linked_profile_id=sid,
        )
        slots.append(slot)
        out = FlowCdpSettings(
            slots=slots,
            retired_ports=sorted(retired_ports),
            retired_slot_ids=sorted(retired_ids),
        ).normalized()
        _persist(out.to_dict())
        for s in out.slots:
            if s.id == sid:
                return s
        return slot


def remove_flow_cdp_slot(slot_id: str) -> FlowCdpSettings:
    """Xóa slot khỏi danh sách và retire port + id (không tái cấp)."""
    sid = str(slot_id or "").strip()
    if not sid:
        raise ValueError("invalid_slot_id")
    with _LOCK:
        current = _settings_from_raw(_load_raw())
        removed: FlowCdpSlot | None = None
        slots: list[FlowCdpSlot] = []
        for s in current.slots:
            if s.id == sid:
                removed = s
            else:
                slots.append(s)
        if not removed:
            raise KeyError("slot_not_found")
        retired_ports = set(current.retired_ports)
        retired_ids = set(current.retired_slot_ids)
        retired_ports.add(int(removed.port))
        retired_ids.add(removed.id)
        out = FlowCdpSettings(
            slots=slots,
            retired_ports=sorted(retired_ports),
            retired_slot_ids=sorted(retired_ids),
        ).normalized()
        _persist(out.to_dict())
        return out


def update_flow_cdp_slot(
    slot_id: str,
    *,
    label: str | None = None,
    port: int | None = None,
    role: str | None = None,
    email: str | None = None,
    linked_profile_id: str | None = None,
) -> FlowCdpSlot:
    sid = str(slot_id or "").strip()
    with _LOCK:
        current = _settings_from_raw(_load_raw())
        slots = list(current.slots)
        found: FlowCdpSlot | None = None
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
            retired = set(current.retired_ports)
            if new_port in used or new_port in retired:
                raise ValueError("port_in_use_or_retired")
            found.port = new_port
        if role is not None:
            role_n = str(role).strip().lower()
            if role_n in ("bridge", "center"):
                found.role = role_n
        if email is not None:
            found.email = str(email).strip()
        if linked_profile_id is not None:
            found.linked_profile_id = str(linked_profile_id).strip() or found.id
        out = FlowCdpSettings(
            slots=slots,
            retired_ports=list(current.retired_ports),
            retired_slot_ids=list(current.retired_slot_ids),
        ).normalized()
        _persist(out.to_dict())
        for s in out.slots:
            if s.id == sid:
                return s
        raise KeyError("slot_not_found")


def is_flow_cdp_slot_id(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid:
        return False
    if get_flow_cdp_slot(pid):
        return True
    for s in list_flow_cdp_slots():
        if s.profile_id() == pid:
            return True
    return False
