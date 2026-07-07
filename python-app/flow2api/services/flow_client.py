"""
Bridge tới Chrome Extension (Flow2API Bridge) — multi-profile pool.

Mỗi Chrome profile cài cùng extension, kết nối WS ws://127.0.0.1:1609.
Agent phân bổ task round-robin qua ExtensionPool.
"""
from __future__ import annotations

from typing import Any, Optional

from flow2api.services.extension_pool import ExtensionSession, get_extension_pool
from flow2api.services.request_params import get_video_quality

# Back-compat alias
FlowClient = ExtensionSession


def get_flow_client() -> ExtensionSession:
    """Session đang bind cho worker task, hoặc profile ready đầu tiên."""
    pool = get_extension_pool()
    bound = pool.get_bound()
    if bound and bound.connected:
        return bound
    return _first_ready_or_offline(pool)


def get_flow_client_for_profile(profile_id: Optional[str] = None) -> ExtensionSession:
    """Session cho HTTP upscale — ưu tiên profile_id đã tạo ảnh (multi-account)."""
    pool = get_extension_pool()
    pid = str(profile_id or "").strip()
    if pid:
        session = pool.get(pid)
        if session and session.is_ready():
            return session
        raise RuntimeError("profile_not_ready")
    return _first_ready_or_offline(pool)


def _first_ready_or_offline(pool: Any) -> ExtensionSession:
    ready = pool.first_ready()
    if ready:
        return ready
    offline = pool.get("_offline")
    if not offline:
        offline = ExtensionSession("_offline", "Chưa có profile")
        pool._sessions["_offline"] = offline  # noqa: SLF001
    return offline


def bind_task_profile(profile_id: str) -> None:
    get_extension_pool().bind_profile(profile_id)


def unbind_task_profile() -> None:
    get_extension_pool().unbind_profile()


_OMNI_VIDEO_TYPES = frozenset({"gen_text_video", "gen_image_video"})

_IMAGE_REQUEST_TYPES = frozenset({"gen_image", "upsample_image"})
_VIDEO_REQUEST_TYPES = frozenset({
    "gen_text_video",
    "gen_image_video",
    "gen_video",
    "gen_video_start_end",
    "gen_multi_image_video",
    "upsample_video",
})


def request_media_kind(request_type: str | None) -> str | None:
    rtype = str(request_type or "").lower().strip()
    if rtype in _IMAGE_REQUEST_TYPES:
        return "image"
    if rtype in _VIDEO_REQUEST_TYPES:
        return "video"
    return None


def profile_accepts_request_type(profile_id: str, request_type: str | None) -> bool:
    from flow2api.services.worker_settings import (
        is_profile_image_allowed,
        is_profile_video_allowed,
    )

    kind = request_media_kind(request_type)
    if kind == "image":
        return is_profile_image_allowed(profile_id)
    if kind == "video":
        return is_profile_video_allowed(profile_id)
    return is_profile_image_allowed(profile_id) or is_profile_video_allowed(profile_id)


def profile_media_pick_priority(profile_id: str, request_type: str | None) -> int:
    """0 = chỉ Image/Video, 1 = cả hai, 2 = không nhận loại này."""
    from flow2api.services.worker_settings import (
        is_profile_image_allowed,
        is_profile_video_allowed,
    )

    img = is_profile_image_allowed(profile_id)
    vid = is_profile_video_allowed(profile_id)
    kind = request_media_kind(request_type)
    if kind == "image":
        if img and not vid:
            return 0
        if img and vid:
            return 1
        return 2
    if kind == "video":
        if vid and not img:
            return 0
        if vid and img:
            return 1
        return 2
    if img or vid:
        return 1
    return 2


def request_requires_credit_profile(
    params: dict[str, Any],
    request_type: str | None = None,
) -> bool:
    from flow2api.services import flow_sdk

    rtype = str(request_type or "").lower()
    if rtype not in _OMNI_VIDEO_TYPES:
        return False
    return flow_sdk.is_omni_flash(get_video_quality(params))


def _profile_matches_credit_pool(profile_id: str, credit_required: bool) -> bool:
    from flow2api.services.worker_settings import is_profile_credit_allowed

    allowed = is_profile_credit_allowed(profile_id)
    return allowed if credit_required else not allowed


def pick_profile_for_task(
    existing_profile_id: Optional[str] = None,
    *,
    credit_required: bool = False,
    request_type: str | None = None,
) -> Optional[str]:
    from flow2api.services.worker_settings import (
        get_profile_max_concurrent,
        is_profile_dispatch_enabled,
    )

    pool = get_extension_pool()
    if existing_profile_id:
        pid = str(existing_profile_id).strip()
        if (
            is_profile_dispatch_enabled(pid)
            and _profile_matches_credit_pool(pid, credit_required)
            and profile_accepts_request_type(pid, request_type)
        ):
            session = pool.get(pid)
            if session and session.is_ready():
                limit = get_profile_max_concurrent(pid)
                if session.active_jobs < limit:
                    return pid
        return None
    return pool.pick_round_robin(credit_required=credit_required, request_type=request_type)


def pick_profile_for_retry(
    current_profile_id: str,
    *,
    credit_required: bool = False,
    request_type: str | None = None,
) -> Optional[str]:
    return get_extension_pool().pick_profile_for_retry(
        current_profile_id,
        credit_required=credit_required,
        request_type=request_type,
    )


def apply_user_profile_assignment(
    params: dict[str, Any],
    profile_id: Optional[str],
) -> dict[str, Any]:
    """Pin task to a Chrome profile, or clear pin when profile_id is empty."""
    out = dict(params or {})
    pid = str(profile_id or "").strip()
    if pid:
        session = get_extension_pool().get(pid)
        if not session or pid.startswith("_"):
            raise ValueError("profile_not_found")
        out["profile_id"] = pid
        out["profile_assigned_by_user"] = True
        out["profile_label"] = session.display_name()
        if session.email:
            out["profile_email"] = session.email
        else:
            out.pop("profile_email", None)
    else:
        for key in ("profile_id", "profile_label", "profile_email", "profile_assigned_by_user"):
            out.pop(key, None)
    return out


def profile_available_for_queue(
    params: dict[str, Any],
    request_type: str | None = None,
) -> bool:
    from flow2api.services.worker_settings import is_profile_dispatch_enabled

    credit_required = request_requires_credit_profile(params, request_type)
    pid = params.get("profile_id")
    if params.get("profile_assigned_by_user") and pid:
        pinned = str(pid).strip()
        if not is_profile_dispatch_enabled(pinned):
            return False
        return bool(
            pick_profile_for_task(
                pinned,
                credit_required=credit_required,
                request_type=request_type,
            )
        )
    if pid and not is_profile_dispatch_enabled(str(pid)):
        return bool(
            pick_profile_for_task(None, credit_required=credit_required, request_type=request_type)
        )
    if pid:
        if pick_profile_for_task(
            str(pid),
            credit_required=credit_required,
            request_type=request_type,
        ):
            return True
        return bool(
            pick_profile_for_task(None, credit_required=credit_required, request_type=request_type)
        )
    exclude = params.get("retry_exclude_profile_id")
    if exclude:
        return bool(
            pick_profile_for_retry(
                str(exclude),
                credit_required=credit_required,
                request_type=request_type,
            )
        )
    return bool(
        pick_profile_for_task(None, credit_required=credit_required, request_type=request_type)
    )


def apply_retry_profile_rotation(
    params: dict[str, Any],
    request_type: str | None = None,
) -> dict[str, Any]:
    """Advance retry to the next profile in ring order (idle first; same profile after full cycle)."""
    out = dict(params or {})
    current = str(
        out.get("profile_id") or out.get("retry_exclude_profile_id") or ""
    ).strip()
    if not current:
        out.pop("retry_exclude_profile_id", None)
        return out

    pool = get_extension_pool()
    credit_required = request_requires_credit_profile(out, request_type)
    next_id = pool.pick_profile_for_retry(
        current,
        credit_required=credit_required,
        request_type=request_type,
    )

    if not next_id:
        out.pop("profile_id", None)
        out.pop("profile_label", None)
        out.pop("profile_email", None)
        out["retry_exclude_profile_id"] = current
        return out

    if next_id != current:
        for media_key in ("start_media_id", "end_media_id", "reference_media_ids"):
            out.pop(media_key, None)

    session = pool.get(next_id)
    out["profile_id"] = next_id
    if session:
        out["profile_label"] = session.display_name()
        if session.email:
            out["profile_email"] = session.email
        else:
            out.pop("profile_email", None)
    out.pop("retry_exclude_profile_id", None)
    return out
