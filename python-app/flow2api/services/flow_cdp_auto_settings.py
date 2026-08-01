"""Persisted settings for Flow CDP auto-refresh schedule."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from flow2api.config import STORAGE_DIR

_SETTINGS_PATH = STORAGE_DIR / "flow_cdp_auto_settings.json"
_LOCK = threading.Lock()

_FLOW_URL = "https://labs.google/fx/vi/tools/flow"


@dataclass
class FlowCdpAutoSettings:
    enabled: bool = False
    parallel_gen: int = 1
    parallel_center: int = 1
    # Khi Active (token còn lại) của profile nhận job < ngưỡng này (giờ) → mở CDP refresh
    min_active_hours: float = 2.0
    sync_delay_s: float = 5.0
    # Song song generate trên profile sau khi refresh (max_concurrent) + bật Nhận job
    job_parallel: int = 8
    flow_url: str = _FLOW_URL
    slot_order: list[str] = field(default_factory=list)
    slot_enabled: dict[str, bool] = field(default_factory=dict)

    def normalized(self) -> FlowCdpAutoSettings:
        pg = max(0, min(20, int(self.parallel_gen or 0)))
        pc = max(0, min(20, int(self.parallel_center or 0)))
        if pg == 0 and pc == 0:
            pg = 1
        hours = max(0.1, min(48.0, float(self.min_active_hours or 2)))
        sync_d = max(1.0, min(120.0, float(self.sync_delay_s or 5)))
        job_p = max(1, min(30, int(self.job_parallel or 8)))
        url = str(self.flow_url or _FLOW_URL).strip() or _FLOW_URL
        order: list[str] = []
        seen: set[str] = set()
        for sid in self.slot_order or []:
            s = str(sid or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            order.append(s)
        enabled_map: dict[str, bool] = {}
        if isinstance(self.slot_enabled, dict):
            for k, v in self.slot_enabled.items():
                kid = str(k or "").strip()
                if kid:
                    enabled_map[kid] = bool(v)
        return FlowCdpAutoSettings(
            enabled=bool(self.enabled),
            parallel_gen=pg,
            parallel_center=pc,
            min_active_hours=hours,
            sync_delay_s=sync_d,
            job_parallel=job_p,
            flow_url=url,
            slot_order=order,
            slot_enabled=enabled_map,
        )

    def to_dict(self) -> dict[str, Any]:
        n = self.normalized()
        return {
            "enabled": n.enabled,
            "parallel_gen": n.parallel_gen,
            "parallel_center": n.parallel_center,
            "min_active_hours": n.min_active_hours,
            "sync_delay_s": n.sync_delay_s,
            "job_parallel": n.job_parallel,
            "flow_url": n.flow_url,
            "slot_order": list(n.slot_order),
            "slot_enabled": dict(n.slot_enabled),
        }


def _load_raw() -> dict[str, Any]:
    if not _SETTINGS_PATH.is_file():
        return {}
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _from_raw(raw: dict[str, Any]) -> FlowCdpAutoSettings:
    # Migrate old min_interval_s (seconds stagger) → ignore; use min_active_hours
    hours = raw.get("min_active_hours")
    if hours is None:
        hours = 2.0
    job_p = raw.get("job_parallel")
    if job_p is None:
        job_p = 8
    return FlowCdpAutoSettings(
        enabled=bool(raw.get("enabled")),
        parallel_gen=int(raw.get("parallel_gen") if raw.get("parallel_gen") is not None else 1),
        parallel_center=int(
            raw.get("parallel_center") if raw.get("parallel_center") is not None else 1
        ),
        min_active_hours=float(hours),
        sync_delay_s=float(raw.get("sync_delay_s") if raw.get("sync_delay_s") is not None else 5),
        job_parallel=int(job_p),
        flow_url=str(raw.get("flow_url") or _FLOW_URL),
        slot_order=list(raw.get("slot_order") or []),
        slot_enabled=dict(raw.get("slot_enabled") or {}),
    ).normalized()


def _persist(data: dict[str, Any]) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_flow_cdp_auto_settings() -> FlowCdpAutoSettings:
    with _LOCK:
        return _from_raw(_load_raw())


def save_flow_cdp_auto_settings(**fields: Any) -> FlowCdpAutoSettings:
    with _LOCK:
        data = _from_raw(_load_raw()).to_dict()
        for key in (
            "enabled",
            "parallel_gen",
            "parallel_center",
            "min_active_hours",
            "sync_delay_s",
            "job_parallel",
            "flow_url",
            "slot_order",
            "slot_enabled",
        ):
            if key in fields and fields[key] is not None:
                data[key] = fields[key]
        # Drop legacy key if present
        data.pop("min_interval_s", None)
        out = _from_raw(data)
        _persist(out.to_dict())
        return out
