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

_MAX_CONCURRENT_CAP = 100
_PROFILE_MAX_CAP = 30


@dataclass
class WorkerSettings:
    max_concurrent: int = 1
    task_stagger_s: float = 0.0
    profile_default_max_concurrent: int = 1
    profile_limits: dict[str, int] = field(default_factory=dict)
    profile_dispatch_disabled: list[str] = field(default_factory=list)
    # User click «Ngừng job» — auto CDP không thay thế / không mở lại
    profile_manual_dispatch_off: list[str] = field(default_factory=list)
    profile_credit_allowed: list[str] = field(default_factory=list)
    profile_image_allowed: list[str] = field(default_factory=list)
    profile_video_allowed: list[str] = field(default_factory=list)
    profile_forgotten: list[str] = field(default_factory=list)
    captcha_center_forgotten: list[str] = field(default_factory=list)

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
        disabled: list[str] = []
        if isinstance(self.profile_dispatch_disabled, list):
            for pid in self.profile_dispatch_disabled:
                if pid and not str(pid).startswith("_"):
                    disabled.append(str(pid))
        manual_off: list[str] = []
        if isinstance(self.profile_manual_dispatch_off, list):
            for pid in self.profile_manual_dispatch_off:
                if pid and not str(pid).startswith("_"):
                    manual_off.append(str(pid))
        credit_allowed: list[str] = []
        if isinstance(self.profile_credit_allowed, list):
            for pid in self.profile_credit_allowed:
                if pid and not str(pid).startswith("_"):
                    credit_allowed.append(str(pid))
        image_allowed: list[str] = []
        if isinstance(self.profile_image_allowed, list):
            for pid in self.profile_image_allowed:
                if pid and not str(pid).startswith("_"):
                    image_allowed.append(str(pid))
        video_allowed: list[str] = []
        if isinstance(self.profile_video_allowed, list):
            for pid in self.profile_video_allowed:
                if pid and not str(pid).startswith("_"):
                    video_allowed.append(str(pid))
        forgotten: list[str] = []
        if isinstance(self.profile_forgotten, list):
            for pid in self.profile_forgotten:
                if pid and not str(pid).startswith("_"):
                    forgotten.append(str(pid))
        captcha_forgotten: list[str] = []
        if isinstance(self.captcha_center_forgotten, list):
            for cid in self.captcha_center_forgotten:
                if cid and not str(cid).startswith("_"):
                    captcha_forgotten.append(str(cid))
        return WorkerSettings(
            max_concurrent=mc,
            task_stagger_s=stagger,
            profile_default_max_concurrent=default_p,
            profile_limits=limits,
            profile_dispatch_disabled=sorted(set(disabled)),
            profile_manual_dispatch_off=sorted(set(manual_off)),
            profile_credit_allowed=sorted(set(credit_allowed)),
            profile_image_allowed=sorted(set(image_allowed)),
            profile_video_allowed=sorted(set(video_allowed)),
            profile_forgotten=sorted(set(forgotten)),
            captcha_center_forgotten=sorted(set(captcha_forgotten)),
        )

    def to_dict(self) -> dict[str, Any]:
        n = self.normalized()
        return {
            "max_concurrent": n.max_concurrent,
            "task_stagger_s": n.task_stagger_s,
            "profile_default_max_concurrent": n.profile_default_max_concurrent,
            "profile_limits": dict(n.profile_limits),
            "profile_dispatch_disabled": list(n.profile_dispatch_disabled),
            "profile_manual_dispatch_off": list(n.profile_manual_dispatch_off),
            "profile_credit_allowed": list(n.profile_credit_allowed),
            "profile_image_allowed": list(n.profile_image_allowed),
            "profile_video_allowed": list(n.profile_video_allowed),
            "profile_forgotten": list(n.profile_forgotten),
            "captcha_center_forgotten": list(n.captcha_center_forgotten),
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
    disabled = raw.get("profile_dispatch_disabled") or []
    if not isinstance(disabled, list):
        disabled = []
    credit_allowed = raw.get("profile_credit_allowed") or []
    if not isinstance(credit_allowed, list):
        credit_allowed = []
    image_allowed = raw.get("profile_image_allowed") or []
    if not isinstance(image_allowed, list):
        image_allowed = []
    video_allowed = raw.get("profile_video_allowed") or []
    if not isinstance(video_allowed, list):
        video_allowed = []
    forgotten = raw.get("profile_forgotten") or []
    if not isinstance(forgotten, list):
        forgotten = []
    captcha_forgotten = raw.get("captcha_center_forgotten") or []
    if not isinstance(captcha_forgotten, list):
        captcha_forgotten = []
    manual_off = raw.get("profile_manual_dispatch_off") or []
    if not isinstance(manual_off, list):
        manual_off = []
    return WorkerSettings(
        max_concurrent=int(raw.get("max_concurrent", 1)),
        task_stagger_s=float(raw.get("task_stagger_s", 0)),
        profile_default_max_concurrent=int(raw.get("profile_default_max_concurrent", 1)),
        profile_limits={str(k): int(v) for k, v in limits.items()},
        profile_dispatch_disabled=[str(x) for x in disabled],
        profile_manual_dispatch_off=[str(x) for x in manual_off],
        profile_credit_allowed=[str(x) for x in credit_allowed],
        profile_image_allowed=[str(x) for x in image_allowed],
        profile_video_allowed=[str(x) for x in video_allowed],
        profile_forgotten=[str(x) for x in forgotten],
        captcha_center_forgotten=[str(x) for x in captcha_forgotten],
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


def is_profile_dispatch_enabled(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    settings = get_worker_settings()
    if pid in settings.profile_forgotten:
        return False
    return pid not in settings.profile_dispatch_disabled


def is_profile_forgotten(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    return pid in get_worker_settings().profile_forgotten


def forget_profile(profile_id: str) -> WorkerSettings:
    """Ẩn profile khỏi dashboard/dispatch cho đến khi rediscover."""
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    current = get_worker_settings()
    forgotten = [x for x in current.profile_forgotten if x != pid]
    forgotten.append(pid)
    limits = dict(current.profile_limits)
    limits.pop(pid, None)
    disabled = [x for x in current.profile_dispatch_disabled if x != pid]
    credit = [x for x in current.profile_credit_allowed if x != pid]
    image = [x for x in current.profile_image_allowed if x != pid]
    video = [x for x in current.profile_video_allowed if x != pid]
    manual_off = [x for x in current.profile_manual_dispatch_off if x != pid]
    return save_worker_settings(
        profile_forgotten=forgotten,
        profile_limits=limits,
        profile_dispatch_disabled=disabled,
        profile_manual_dispatch_off=manual_off,
        profile_credit_allowed=credit,
        profile_image_allowed=image,
        profile_video_allowed=video,
    )


def purge_profile(profile_id: str) -> WorkerSettings:
    """Xóa hẳn profile khỏi mọi danh sách worker (không soft-forget)."""
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    current = get_worker_settings()
    limits = dict(current.profile_limits)
    limits.pop(pid, None)
    return save_worker_settings(
        profile_forgotten=[x for x in current.profile_forgotten if x != pid],
        profile_limits=limits,
        profile_dispatch_disabled=[x for x in current.profile_dispatch_disabled if x != pid],
        profile_manual_dispatch_off=[x for x in current.profile_manual_dispatch_off if x != pid],
        profile_credit_allowed=[x for x in current.profile_credit_allowed if x != pid],
        profile_image_allowed=[x for x in current.profile_image_allowed if x != pid],
        profile_video_allowed=[x for x in current.profile_video_allowed if x != pid],
    )


def unforget_profile(profile_id: str) -> WorkerSettings:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    current = get_worker_settings()
    forgotten = [x for x in current.profile_forgotten if x != pid]
    return save_worker_settings(profile_forgotten=forgotten)


def is_captcha_center_forgotten(center_id: str) -> bool:
    cid = str(center_id or "").strip()
    if not cid:
        return False
    return cid in get_worker_settings().captcha_center_forgotten


def forget_captcha_center(center_id: str) -> WorkerSettings:
    cid = str(center_id or "").strip()
    if not cid:
        raise ValueError("invalid_center_id")
    current = get_worker_settings()
    forgotten = [x for x in current.captcha_center_forgotten if x != cid]
    forgotten.append(cid)
    return save_worker_settings(captcha_center_forgotten=forgotten)


def unforget_captcha_center(center_id: str) -> WorkerSettings:
    cid = str(center_id or "").strip()
    if not cid:
        raise ValueError("invalid_center_id")
    current = get_worker_settings()
    forgotten = [x for x in current.captcha_center_forgotten if x != cid]
    return save_worker_settings(captcha_center_forgotten=forgotten)


def set_profile_dispatch_enabled(
    profile_id: str,
    enabled: bool,
    *,
    source: str = "manual",
) -> WorkerSettings:
    """Bật/tắt nhận job.

    source:
      - ``manual``: user bấm Ngừng/Bật job trên UI — auto CDP không thay thế profile này
      - ``system``: auto (token/auth/quota/standby) — auto CDP được quyền đổi Gen
    """
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    src = str(source or "manual").strip().lower()
    if src not in ("manual", "system"):
        src = "manual"
    current = get_worker_settings()
    disabled = [x for x in current.profile_dispatch_disabled if x != pid]
    manual_off = [x for x in current.profile_manual_dispatch_off if x != pid]
    if not enabled:
        disabled.append(pid)
        if src == "manual":
            manual_off.append(pid)
        # system off: không đưa vào manual_off (và xóa nếu từng manual)
    # enabled=True: đã xóa khỏi manual_off + disabled
    return save_worker_settings(
        profile_dispatch_disabled=disabled,
        profile_manual_dispatch_off=manual_off,
    )


def is_profile_manual_dispatch_off(profile_id: str) -> bool:
    """True nếu user đã bấm Ngừng job thủ công (auto CDP không được mở lại)."""
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    return pid in get_worker_settings().profile_manual_dispatch_off


def is_profile_credit_allowed(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    return pid in get_worker_settings().profile_credit_allowed


def set_profile_credit_allowed(profile_id: str, allowed: bool) -> WorkerSettings:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    current = get_worker_settings()
    allowed_ids = [x for x in current.profile_credit_allowed if x != pid]
    if allowed:
        allowed_ids.append(pid)
    return save_worker_settings(profile_credit_allowed=allowed_ids)


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
        if "profile_dispatch_disabled" in fields:
            raw_disabled = fields["profile_dispatch_disabled"]
            if isinstance(raw_disabled, list):
                data["profile_dispatch_disabled"] = [
                    str(x)
                    for x in raw_disabled
                    if x and not str(x).startswith("_")
                ]
        if "profile_manual_dispatch_off" in fields:
            raw_manual = fields["profile_manual_dispatch_off"]
            if isinstance(raw_manual, list):
                data["profile_manual_dispatch_off"] = [
                    str(x)
                    for x in raw_manual
                    if x and not str(x).startswith("_")
                ]
        if "profile_credit_allowed" in fields:
            raw_credit = fields["profile_credit_allowed"]
            if isinstance(raw_credit, list):
                data["profile_credit_allowed"] = [
                    str(x)
                    for x in raw_credit
                    if x and not str(x).startswith("_")
                ]
        if "profile_image_allowed" in fields:
            raw_image = fields["profile_image_allowed"]
            if isinstance(raw_image, list):
                data["profile_image_allowed"] = [
                    str(x)
                    for x in raw_image
                    if x and not str(x).startswith("_")
                ]
        if "profile_video_allowed" in fields:
            raw_video = fields["profile_video_allowed"]
            if isinstance(raw_video, list):
                data["profile_video_allowed"] = [
                    str(x)
                    for x in raw_video
                    if x and not str(x).startswith("_")
                ]
        if "profile_forgotten" in fields:
            raw_forgotten = fields["profile_forgotten"]
            if isinstance(raw_forgotten, list):
                data["profile_forgotten"] = [
                    str(x)
                    for x in raw_forgotten
                    if x and not str(x).startswith("_")
                ]
        if "captcha_center_forgotten" in fields:
            raw_cf = fields["captcha_center_forgotten"]
            if isinstance(raw_cf, list):
                data["captcha_center_forgotten"] = [
                    str(x) for x in raw_cf if x and not str(x).startswith("_")
                ]
        out = WorkerSettings(**data).normalized()
        return _write_settings(out)


def save_profile_limit(profile_id: str, max_concurrent: int) -> WorkerSettings:
    return save_worker_settings(profile_limits={str(profile_id): int(max_concurrent)})


def is_profile_image_allowed(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    return pid in get_worker_settings().profile_image_allowed


def is_profile_video_allowed(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    return pid in get_worker_settings().profile_video_allowed


def set_profile_media_allowed(
    profile_id: str,
    *,
    image: bool | None = None,
    video: bool | None = None,
) -> WorkerSettings:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    current = get_worker_settings()
    image_ids = [x for x in current.profile_image_allowed if x != pid]
    video_ids = [x for x in current.profile_video_allowed if x != pid]
    if image is True:
        image_ids.append(pid)
    elif image is None and pid in current.profile_image_allowed:
        image_ids.append(pid)
    if video is True:
        video_ids.append(pid)
    elif video is None and pid in current.profile_video_allowed:
        video_ids.append(pid)
    return save_worker_settings(
        profile_image_allowed=sorted(set(image_ids)),
        profile_video_allowed=sorted(set(video_ids)),
    )


def all_known_profile_ids() -> list[str]:
    settings = get_worker_settings()
    ids: set[str] = set()
    ids.update(settings.profile_limits.keys())
    ids.update(settings.profile_image_allowed)
    ids.update(settings.profile_video_allowed)
    ids.update(settings.profile_dispatch_disabled)
    ids.update(settings.profile_credit_allowed)
    try:
        from flow2api.services.extension_pool import get_extension_pool

        for session in get_extension_pool().list_sessions():
            pid = str(session.profile_id or "").strip()
            if pid and not pid.startswith("_"):
                ids.add(pid)
    except Exception:
        pass
    return sorted(ids)


def seed_profile_media_alternating(profile_ids: list[str] | None = None) -> WorkerSettings:
    """Gán tất cả profile vào cả Image lẫn Video."""
    pids = sorted(
        {
            str(p).strip()
            for p in (profile_ids if profile_ids is not None else all_known_profile_ids())
            if p and not str(p).startswith("_")
        }
    )
    if not pids:
        return get_worker_settings()
    return save_worker_settings(
        profile_image_allowed=list(pids),
        profile_video_allowed=list(pids),
    )


def ensure_profile_media_on_connect(profile_id: str) -> WorkerSettings:
    """Profile mới kết nối: bật cả Image và Video, không đổi profile cũ."""
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return get_worker_settings()
    settings = get_worker_settings()
    if pid in settings.profile_image_allowed and pid in settings.profile_video_allowed:
        return settings
    image_ids = list(settings.profile_image_allowed)
    video_ids = list(settings.profile_video_allowed)
    if pid not in image_ids:
        image_ids.append(pid)
    if pid not in video_ids:
        video_ids.append(pid)
    return save_worker_settings(
        profile_image_allowed=sorted(set(image_ids)),
        profile_video_allowed=sorted(set(video_ids)),
    )


def bootstrap_profile_media_on_startup() -> WorkerSettings:
    pids = all_known_profile_ids()
    if not pids:
        return get_worker_settings()
    return seed_profile_media_alternating(pids)
