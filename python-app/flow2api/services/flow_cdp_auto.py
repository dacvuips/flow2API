"""Flow CDP auto schedule: open → clear synced cookies → click Flow UI → sync → close."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from flow2api.services import system_ops
from flow2api.services.flow_cdp_auto_settings import (
    get_flow_cdp_auto_settings,
    save_flow_cdp_auto_settings,
)
from flow2api.services.flow_cdp_control import sync_session
from flow2api.services.flow_cdp_settings import get_flow_cdp_slot, list_flow_cdp_slots

logger = logging.getLogger(__name__)

_DEFAULT_FLOW_URL = "https://labs.google/fx/vi/tools/flow"

_lock = asyncio.Lock()
_scheduler_task: asyncio.Task | None = None
_running: dict[str, dict[str, Any]] = {}  # slot_id -> meta
_pending_start: set[str] = set()  # đã schedule, chưa vào cycle
_logs: list[dict[str, Any]] = []
_MAX_LOGS = 80
_fail_cooldown_until: dict[str, float] = {}  # slot_id -> unix ts
_FAIL_COOLDOWN_S = 15 * 60
_success_cooldown_until: dict[str, float] = {}  # slot_id -> unix ts (sau sync thành công)


def _log(level: str, message: str, **extra: Any) -> None:
    item = {
        "ts": time.time(),
        "level": level,
        "message": message,
        **extra,
    }
    _logs.append(item)
    if len(_logs) > _MAX_LOGS:
        del _logs[: len(_logs) - _MAX_LOGS]
    if level == "error":
        logger.error("flow-cdp-auto: %s", message)
    else:
        logger.info("flow-cdp-auto: %s", message)


def _slots_with_email() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in list_flow_cdp_slots():
        email = str(s.email or "").strip()
        if not email:
            continue
        out.append(
            {
                "id": s.id,
                "email": email,
                "label": s.label or email,
                "role": s.role if s.role in ("bridge", "center") else "bridge",
                "port": s.port,
                "cdp_url": s.cdp_url(),
                "linked_profile_id": s.profile_id(),
            }
        )
    return out


def _profile_token_meta(profile_id: str) -> dict[str, Any]:
    from flow2api.services.flow_profile_service import token_public_fields
    from flow2api.services.worker_settings import is_profile_dispatch_enabled
    from flow2api.services.extension_pool import get_extension_pool

    pid = str(profile_id or "").strip()
    meta = token_public_fields(pid) if pid else {}
    dispatch = True
    accepting = False
    try:
        dispatch = is_profile_dispatch_enabled(pid)
    except Exception:
        dispatch = True
    try:
        session = get_extension_pool().get(pid)
        if session:
            accepting = bool(
                dispatch
                and (session.is_ready() or int(getattr(session, "active_jobs", 0) or 0) > 0)
            )
        else:
            # DB-only / offline gen profile
            accepting = bool(dispatch and meta.get("direct_lane_ready"))
    except Exception:
        accepting = bool(dispatch)
    rem = meta.get("token_remaining_seconds")
    rem_real = meta.get("token_remaining_seconds_real")
    if rem_real is None:
        rem_real = rem
    return {
        "token_remaining_seconds": rem,
        "token_remaining_seconds_real": rem_real,
        "token_hours_left": meta.get("token_hours_left"),
        "token_status": meta.get("token_status"),
        "access_token_expires_at": meta.get("access_token_expires_at"),
        "dispatch_enabled": dispatch,
        "accepting_jobs": accepting,
        # Standby = có trong danh sách auto nhưng đang ngưng nhận job → sẵn sàng được mở khi lỗi tài khoản
        "standby": bool(not dispatch),
    }


def ordered_auto_slots() -> list[dict[str, Any]]:
    """CDPs có email, theo slot_order (kéo thả), kèm trạng thái nhận job / standby."""
    cfg = get_flow_cdp_auto_settings()
    by_id = {s["id"]: s for s in _slots_with_email()}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _enrich(base: dict[str, Any]) -> dict[str, Any]:
        item = dict(base)
        item["enabled"] = cfg.slot_enabled.get(item["id"], True)
        tok = _profile_token_meta(item.get("linked_profile_id") or item["id"])
        item.update(tok)
        item["below_active_threshold"] = False  # legacy field — không còn dùng Active
        return item

    for sid in cfg.slot_order:
        if sid in by_id and sid not in seen:
            ordered.append(_enrich(by_id[sid]))
            seen.add(sid)
    for sid, item in by_id.items():
        if sid in seen:
            continue
        ordered.append(_enrich(item))
    return ordered


def auto_status() -> dict[str, Any]:
    cfg = get_flow_cdp_auto_settings()
    slots = ordered_auto_slots()
    running = []
    for sid, meta in list(_running.items()):
        running.append(
            {
                "slot_id": sid,
                "email": meta.get("email"),
                "role": meta.get("role"),
                "started_at": meta.get("started_at"),
                "step": meta.get("step"),
            }
        )
    accepting = [s for s in slots if s.get("enabled") and s.get("accepting_jobs")]
    # Standby + «không nhận job» (dispatch ON nhưng không accepting) đều coi là chờ mở CDP
    openable = [
        s
        for s in slots
        if s.get("enabled")
        and (s.get("role") or "bridge") != "center"
        and not s.get("accepting_jobs")
    ]
    return {
        "ok": True,
        "settings": cfg.to_dict(),
        "slots": slots,
        "running": running,
        "running_count": len(_running),
        "need_refresh_count": 0,
        "accepting_count": len(accepting),
        "standby_count": len(openable),
        "logs": list(_logs[-40:]),
        "scheduler_alive": bool(_scheduler_task and not _scheduler_task.done()),
        "hint": (
            "Nút «Chạy CDP tiếp theo»: lấy CDP Gen standby / «không nhận job» kế tiếp tuần hoàn "
            "→ Sync cookies → bật Nhận job. "
            "Sau «Lưu cấu hình»: nếu số Gen đang nhận job < Song song Gen CDP → tự mở CDP cho đủ "
            "(Standby và «không nhận job» đều mở được). "
            "Khi lịch bật và gen lỗi TK / token hết hạn → Ngừng job → mở CDP kế tuần hoàn "
            "(flow12 → flow1 → …). HTTP 403/429 thoáng qua không đổi Gen."
        ),
    }


def _find_slot_index_for_profile(slots: list[dict[str, Any]], profile_id: str) -> int:
    pid = str(profile_id or "").strip()
    if not pid:
        return -1
    for i, s in enumerate(slots):
        linked = str(s.get("linked_profile_id") or s.get("id") or "").strip()
        sid = str(s.get("id") or "").strip()
        if linked == pid or sid == pid:
            return i
    return -1


def _last_activated_gen_index(slots: list[dict[str, Any]]) -> int:
    """Index Gen cuối cùng đang thực sự nhận job (accepting), fallback dispatch ON."""
    last = -1
    last_dispatch = -1
    for i, s in enumerate(slots):
        if not s.get("enabled"):
            continue
        if (s.get("role") or "bridge") == "center":
            continue
        if s.get("accepting_jobs"):
            last = i
        elif s.get("dispatch_enabled"):
            last_dispatch = i
    return last if last >= 0 else last_dispatch


def _is_cycle_busy(slot_id: str) -> bool:
    sid = str(slot_id or "").strip()
    return bool(sid and (sid in _running or sid in _pending_start))


def _gen_slot_eligible(
    s: dict[str, Any],
    *,
    exclude: str = "",
    skip_running: bool = False,
    skip_cooldown: bool = False,
    now: float | None = None,
) -> bool:
    """True nếu slot Gen có thể mở CDP/Sync.

    Gồm:
    - Standby (ngưng job / dispatch OFF) do hệ thống
    - «không nhận job» (dispatch ON nhưng không accepting — token chết, lane hỏng…)
    Không gồm:
    - Gen đang «Nhận job» (accepting)
    - Profile user bấm Ngừng job thủ công (không auto mở lại)
    """
    if not s.get("enabled"):
        return False
    if (s.get("role") or "bridge") == "center":
        return False
    sid = str(s.get("id") or "").strip()
    linked = str(s.get("linked_profile_id") or sid).strip()
    if not sid:
        return False
    if exclude and (linked == exclude or sid == exclude):
        return False
    if skip_running and _is_cycle_busy(sid):
        return False
    ts = time.time() if now is None else now
    if skip_cooldown and _fail_cooldown_until.get(sid, 0) > ts:
        return False
    # User bấm Ngừng job → không auto mở CDP / không bù song song bằng slot này
    try:
        from flow2api.services.worker_settings import is_profile_manual_dispatch_off

        if is_profile_manual_dispatch_off(linked) or is_profile_manual_dispatch_off(sid):
            return False
    except Exception:
        pass
    # Đang nhận job thật → bỏ qua; standby và «không nhận job» (hệ thống) đều lấy được
    if s.get("accepting_jobs"):
        return False
    return True


def _count_manual_dispatch_off_gens() -> int:
    """Số Gen trong lịch auto mà user đã Ngừng job thủ công (giảm mục tiêu song song)."""
    try:
        from flow2api.services.worker_settings import is_profile_manual_dispatch_off
    except Exception:
        return 0
    n = 0
    for s in ordered_auto_slots():
        if not s.get("enabled"):
            continue
        if (s.get("role") or "bridge") == "center":
            continue
        sid = str(s.get("id") or "").strip()
        linked = str(s.get("linked_profile_id") or sid).strip()
        if not sid:
            continue
        try:
            if is_profile_manual_dispatch_off(linked) or is_profile_manual_dispatch_off(sid):
                n += 1
        except Exception:
            continue
    return n


def _parallel_gen_target() -> int:
    """Song song Gen CDP trừ số profile user cố ý ngừng (không bù thay)."""
    cfg = get_flow_cdp_auto_settings()
    target = int(cfg.parallel_gen or 0)
    if target <= 0:
        return 0
    manual_n = _count_manual_dispatch_off_gens()
    return max(0, target - manual_n)


def _next_gen_standby_after(
    slots: list[dict[str, Any]],
    *,
    after_index: int,
    exclude_profile_id: str = "",
    skip_running: bool = False,
    skip_cooldown: bool = False,
) -> dict[str, Any] | None:
    """CDP Gen standby / không nhận job ngay dưới after_index (không wrap)."""
    exclude = str(exclude_profile_id or "").strip()
    start = max(-1, int(after_index))
    now = time.time()
    for s in slots[start + 1 :]:
        if _gen_slot_eligible(
            s,
            exclude=exclude,
            skip_running=skip_running,
            skip_cooldown=skip_cooldown,
            now=now,
        ):
            return s
    return None


def _next_gen_standby_circular(
    slots: list[dict[str, Any]],
    *,
    after_index: int,
    exclude_profile_id: str = "",
    skip_running: bool = True,
    skip_cooldown: bool = True,
) -> dict[str, Any] | None:
    """Gen standby / không nhận job kế tiếp tuần hoàn: … → flow12 → flow1 → …

    after_index < 0 → quét 0..n-1 một lượt.
    after_index = index profile vừa lỗi/đang active → bắt đầu từ slot kế, wrap hết list.
    """
    n = len(slots)
    if n == 0:
        return None
    exclude = str(exclude_profile_id or "").strip()
    now = time.time()
    start = int(after_index)
    if start < 0:
        order = list(range(n))
    else:
        # (start+1) … n-1, 0 … start  (có wrap về đầu)
        order = [(start + i) % n for i in range(1, n + 1)]
    for idx in order:
        s = slots[idx]
        if _gen_slot_eligible(
            s,
            exclude=exclude,
            skip_running=skip_running,
            skip_cooldown=skip_cooldown,
            now=now,
        ):
            return s
    return None


def find_next_standby_gen_slot(failed_profile_id: str) -> dict[str, Any] | None:
    """CDP Gen tiếp theo sau profile vừa lỗi — tuần hoàn (standby + không nhận job).

    1) Quét vòng từ ngay dưới profile lỗi (flow12 → flow1 → …)
    2) Nếu tất cả đang cooldown fail 15p → quét lại bỏ cooldown (vẫn tuần hoàn)
    """
    slots = ordered_auto_slots()
    exclude = str(failed_profile_id or "").strip()
    start_idx = _find_slot_index_for_profile(slots, exclude) if exclude else -1
    if start_idx < 0:
        start_idx = _last_activated_gen_index(slots)
    nxt = _next_gen_standby_circular(
        slots,
        after_index=start_idx,
        exclude_profile_id=exclude,
        skip_running=True,
        skip_cooldown=True,
    )
    if nxt:
        return nxt
    # Vòng 2: cho phép reuse profile đã fail gần đây để tuần hoàn không đứt
    return _next_gen_standby_circular(
        slots,
        after_index=start_idx,
        exclude_profile_id=exclude,
        skip_running=True,
        skip_cooldown=False,
    )


def find_next_cdp_to_run() -> dict[str, Any] | None:
    """CDP Gen kế tiếp (standby hoặc «không nhận job») — tuần hoàn flow12 → flow1.

    1) Ngay dưới Gen đang nhận job cuối cùng (wrap hết list)
    2) Bỏ cooldown fail nếu vòng 1 không còn slot
    """
    slots = ordered_auto_slots()
    start_idx = _last_activated_gen_index(slots)
    nxt = _next_gen_standby_circular(
        slots,
        after_index=start_idx,
        skip_running=True,
        skip_cooldown=True,
    )
    if nxt:
        return nxt
    return _next_gen_standby_circular(
        slots,
        after_index=start_idx,
        skip_running=True,
        skip_cooldown=False,
    )


def _count_active_or_starting_gens() -> int:
    """Số Gen đang «giữ chỗ» song song — khớp pill «nhận job» + đang Sync.

    Tính: accepting (Nhận job) · đang/sắp cycle · dispatch ON + token còn hạn.
    Không tính: standby / «không nhận job» (token chết / missing).
    """
    n = 0
    for s in ordered_auto_slots():
        if not s.get("enabled"):
            continue
        if (s.get("role") or "bridge") == "center":
            continue
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        if _is_cycle_busy(sid):
            n += 1
            continue
        if s.get("accepting_jobs"):
            n += 1
            continue
        # Dispatch ON nhưng token còn → vẫn tính đang hoạt động (chưa nhường slot)
        if s.get("dispatch_enabled"):
            status = str(s.get("token_status") or "").strip().lower()
            rem = s.get("token_remaining_seconds_real")
            if rem is None:
                rem = s.get("token_remaining_seconds")
            try:
                rem_n = float(rem) if rem is not None else None
            except (TypeError, ValueError):
                rem_n = None
            if status in ("fresh", "stale") or (rem_n is not None and rem_n > 0):
                n += 1
    return n


def ensure_gen_slots_for_parallel(*, reason: str = "fill_parallel_gen") -> dict[str, Any]:
    """
    Bù CDP Gen cho ĐỦ mục tiêu Song song Gen CDP — chỉ mở thêm (target − have).

    Ví dụ: target=3, đang nhận job=2 → chỉ start đúng 1 CDP (không mở thêm 3).
    Profile user bấm Ngừng job thủ công: không bù thay (effective target giảm).
    """
    cfg = get_flow_cdp_auto_settings()
    raw_target = int(cfg.parallel_gen or 0)
    target = _parallel_gen_target()
    if raw_target <= 0:
        return {
            "ok": True,
            "target": 0,
            "have": 0,
            "need": 0,
            "started": [],
            "skipped": "target_zero",
        }

    started: list[str] = []
    blocked_reason = ""
    have_before = _count_active_or_starting_gens()
    need = max(0, target - have_before)
    if need <= 0:
        return {
            "ok": True,
            "target": target,
            "raw_target": raw_target,
            "manual_off": raw_target - target,
            "have_before": have_before,
            "have": have_before,
            "need": 0,
            "started": [],
            "started_count": 0,
            "blocked_reason": None,
            "skipped": "already_at_target",
        }

    # Chỉ lặp đúng số còn thiếu (không mở tràn target)
    for _ in range(need):
        have = _count_active_or_starting_gens()
        if have >= target:
            break
        remaining = target - have
        if remaining <= 0 or len(started) >= need:
            break
        nxt = find_next_cdp_to_run()
        if not nxt:
            blocked_reason = "no_standby_available"
            break
        sid = str(nxt.get("id") or "").strip()
        if not sid:
            blocked_reason = "empty_slot_id"
            break
        if sid in started:
            blocked_reason = "duplicate_next"
            break
        if not _schedule_cycle(sid, reason=reason):
            # Đạt giới hạn Sync song song / slot busy — chain sẽ bù tiếp sau cycle
            blocked_reason = "schedule_blocked_busy_or_parallel_cap"
            break
        started.append(sid)

    have_after = _count_active_or_starting_gens()
    if started:
        _log(
            "info",
            (
                f"Bù Gen CDP: mục tiêu {target}"
                + (f" (song song={raw_target}, user ngừng={raw_target - target})" if raw_target != target else "")
                + f" · đang có {have_before} · cần +{need} · start {len(started)}: {', '.join(started)} "
                f"· sau schedule={have_after} ({reason})"
            ),
        )
    elif need > 0:
        _log(
            "info",
            (
                f"Bù Gen CDP: cần +{need} (have={have_before} → target={target}) "
                f"nhưng chưa start — {blocked_reason or 'unknown'} ({reason})"
            ),
        )
    return {
        "ok": True,
        "target": target,
        "raw_target": raw_target,
        "manual_off": max(0, raw_target - target),
        "have_before": have_before,
        "have": have_after,
        "need": need,
        "started": started,
        "started_count": len(started),
        "blocked_reason": blocked_reason or None,
    }


def _schedule_cycle(slot_id: str, *, reason: str = "") -> bool:
    """Start run_auto_cycle_for_slot in background if not already running."""
    sid = str(slot_id or "").strip()
    if not sid or _is_cycle_busy(sid):
        return False
    cfg = get_flow_cdp_auto_settings()
    role = "bridge"
    slot = get_flow_cdp_slot(sid)
    if slot:
        role = slot.role if slot.role in ("bridge", "center") else "bridge"
    if role == "center":
        if _count_running_by_role("center") >= int(cfg.parallel_center):
            return False
    else:
        pending_bridge = 0
        for psid in _pending_start:
            ps = get_flow_cdp_slot(psid)
            prole = ps.role if ps and ps.role in ("bridge", "center") else "bridge"
            if prole != "center":
                pending_bridge += 1
        busy = _count_running_by_role("bridge") + pending_bridge
        # Cap Sync song song: tối thiểu 1 để vẫn bù được khi Song song Gen = 0 edge;
        # bình thường = parallel_gen (cùng ô cấu hình mục tiêu số Gen).
        cap = max(1, int(cfg.parallel_gen or 0))
        if busy >= cap:
            return False

    _pending_start.add(sid)

    async def _job(slot_id: str = sid, job_reason: str = reason) -> None:
        ok = False
        err_msg = ""
        try:
            result = await run_auto_cycle_for_slot(slot_id)
            ok = bool(result.get("ok"))
            if ok:
                _fail_cooldown_until.pop(slot_id, None)
                _log(
                    "info",
                    f"Sync xong {slot_id} (trigger: {job_reason or 'manual'})",
                    slot_id=slot_id,
                )
            else:
                err_msg = str(result.get("error") or result.get("message") or "cycle_failed")
                _fail_cooldown_until[slot_id] = time.time() + _FAIL_COOLDOWN_S
                _log(
                    "error",
                    (
                        f"Mở/Sync CDP {slot_id} lỗi: {err_msg} "
                        f"→ bỏ slot (cooldown {_FAIL_COOLDOWN_S // 60}p) · "
                        f"chuyển Gen kế / bù Song song Gen CDP"
                    ),
                    slot_id=slot_id,
                )
        except Exception as exc:
            err_msg = str(exc)
            _fail_cooldown_until[slot_id] = time.time() + _FAIL_COOLDOWN_S
            _log(
                "error",
                (
                    f"Mở/Sync CDP {slot_id} exception: {err_msg} "
                    f"→ chuyển Gen kế / bù Song song Gen CDP"
                ),
                slot_id=slot_id,
            )
        finally:
            _pending_start.discard(slot_id)
            # Thành công hay fail: chỉ bù đúng phần còn thiếu (target − have)
            try:
                chain_reason = (
                    f"chain_ok:{job_reason or 'cycle'}"
                    if ok
                    else f"chain_fail:{slot_id}:{job_reason or 'cycle'}"
                )
                fill = ensure_gen_slots_for_parallel(reason=chain_reason)
                # Fail + vẫn thiếu mục tiêu + ensure chưa start → thử 1 Gen kế (không vượt target)
                tgt = _parallel_gen_target()
                have_now = _count_active_or_starting_gens()
                if (
                    not ok
                    and tgt > 0
                    and have_now < tgt
                    and not (fill.get("started_count") or 0)
                ):
                    nxt = find_next_cdp_to_run()
                    nxt_id = str((nxt or {}).get("id") or "").strip()
                    if nxt_id and nxt_id != slot_id:
                        if _schedule_cycle(nxt_id, reason=f"after_fail:{slot_id}"):
                            _log(
                                "info",
                                (
                                    f"Sau lỗi {slot_id} → mở +1 CDP kế {nxt_id} "
                                    f"(have {have_now}/{tgt}, tuần hoàn)"
                                ),
                                slot_id=nxt_id,
                            )
            except Exception as exc:
                logger.debug("chain ensure_gen_slots_for_parallel: %s", exc)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _pending_start.discard(sid)
        return False
    loop.create_task(_job())
    return True


async def on_profile_http_block(
    failed_profile_id: str,
    *,
    reason: str = "account_error",
) -> dict[str, Any]:
    """
    Khi gen gặp lỗi tài khoản thật (không phải 403/429 thoáng qua):
    - invalid authentication credentials / offline_auth_expired / Token hết hạn / quota exhausted
    - Ngừng nhận job profile lỗi
    - Mở CDP Gen tiếp theo trên danh sách (ngay dưới profile vừa lỗi)
      → Sync + bật Nhận job
    """
    cfg = get_flow_cdp_auto_settings()
    if not cfg.enabled:
        return {"ok": False, "skipped": True, "reason": "auto_disabled"}

    pid = str(failed_profile_id or "").strip()
    if not pid:
        return {"ok": False, "error": "missing_profile_id"}

    # Ngừng nhận job profile lỗi
    try:
        from flow2api.services.worker_settings import set_profile_dispatch_enabled

        set_profile_dispatch_enabled(pid, False, source="system")
        _log("info", f"{reason}: đã Ngừng job profile {pid}")
    except Exception as exc:
        _log("error", f"Ngừng job {pid} thất bại: {exc}")

    # Dừng job đang chạy trên profile lỗi — không tiếp tục refresh/call API
    try:
        from flow2api.worker.processor import get_worker

        moved = get_worker().requeue_running_on_profile(pid, reason=reason)
        if moved:
            _log("info", f"{reason}: đã chuyển {moved} job đang chạy khỏi {pid}")
    except Exception as exc:
        _log("error", f"requeue running off {pid} thất bại: {exc}")

    next_slot = find_next_standby_gen_slot(pid)
    if not next_slot:
        # Fallback cùng finder «Chạy CDP tiếp theo» (đã wrap)
        next_slot = find_next_cdp_to_run()
    if not next_slot:
        # Bù theo Song song Gen CDP (có wrap) — ít nhất 1 slot nếu còn standby
        fill = ensure_gen_slots_for_parallel(reason=f"{reason}:{pid}:no_standby_direct")
        if fill.get("started_count"):
            return {
                "ok": True,
                "failed_profile_id": pid,
                "reason": reason,
                "started": True,
                "fill_parallel": fill,
            }
        _log(
            "info",
            f"{reason} từ {pid}: không còn CDP Gen standby để thay thế",
        )
        return {
            "ok": False,
            "failed_profile_id": pid,
            "reason": reason,
            "error": "no_standby_cdp",
            "fill_parallel": fill,
        }

    next_id = str(next_slot.get("id") or "")
    # Tuần hoàn reuse: bỏ cooldown fail (nếu có) để mở lại flow đầu list
    _fail_cooldown_until.pop(next_id, None)
    if _is_cycle_busy(next_id):
        return {
            "ok": True,
            "failed_profile_id": pid,
            "next_slot_id": next_id,
            "already_running": True,
        }

    # Log rõ khi wrap cuối → đầu
    slots_now = ordered_auto_slots()
    fail_idx = _find_slot_index_for_profile(slots_now, pid)
    next_idx = _find_slot_index_for_profile(slots_now, next_id)
    wrap_note = ""
    if fail_idx >= 0 and next_idx >= 0 and next_idx <= fail_idx:
        wrap_note = " · wrap tuần hoàn"

    _log(
        "info",
        (
            f"{reason} từ {pid} → mở CDP kế tiếp {next_id} "
            f"({next_slot.get('email')}) · Sync + Nhận job{wrap_note}"
        ),
        slot_id=next_id,
    )
    started = _schedule_cycle(next_id, reason=f"{reason}:{pid}")
    if not started:
        _log(
            "error",
            (
                f"{reason} từ {pid}: có standby {next_id} nhưng schedule thất bại "
                f"(đang chạy / chạm cap Sync song song={cfg.parallel_gen})"
            ),
            slot_id=next_id,
        )
    # Giữ số Gen ≈ Song song Gen CDP (1 chết → bù lại nếu target > 1)
    fill: dict[str, Any] = {}
    try:
        fill = ensure_gen_slots_for_parallel(reason=f"{reason}:{pid}:refill")
    except Exception as exc:
        logger.debug("refill after account switch: %s", exc)
    return {
        "ok": started,
        "failed_profile_id": pid,
        "next_slot_id": next_id,
        "next_email": next_slot.get("email"),
        "reason": reason,
        "started": started,
        "fill_parallel": fill or None,
    }


async def _clear_synced_cookies(context, slot_id: str) -> int:
    """
    Clear cookies that Sync session stores (Google/Labs), keep unrelated cookies.
    If DB has a cookie name list, prefer clearing those names on google/labs domains.
    """
    from flow2api.services.cookie_service import get_profile_cookies_raw
    from flow2api.services.flow_cdp_settings import get_flow_cdp_slot

    slot = get_flow_cdp_slot(slot_id)
    pid = slot.profile_id() if slot else slot_id
    raw = get_profile_cookies_raw(pid)
    sync_names: set[str] = set()
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, dict) and c.get("name"):
                sync_names.add(str(c["name"]))

    cookies = await context.cookies()
    keep: list[dict[str, Any]] = []
    removed = 0
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "").lower()
        name = str(c.get("name") or "")
        is_flow = "google" in domain or "labs" in domain
        if is_flow and (not sync_names or name in sync_names):
            removed += 1
            continue
        keep.append(c)

    try:
        await context.clear_cookies()
        if keep:
            # Playwright cookie shape may need url or domain/path
            safe: list[dict[str, Any]] = []
            for c in keep:
                item = {
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain"),
                    "path": c.get("path") or "/",
                }
                if c.get("expires"):
                    item["expires"] = c["expires"]
                if "httpOnly" in c:
                    item["httpOnly"] = c["httpOnly"]
                if "secure" in c:
                    item["secure"] = c["secure"]
                if "sameSite" in c:
                    item["sameSite"] = c["sameSite"]
                if item.get("name") and item.get("value") is not None and item.get("domain"):
                    safe.append(item)
            if safe:
                try:
                    await context.add_cookies(safe)
                except Exception as exc:
                    logger.debug("re-add non-flow cookies failed: %s", exc)
    except Exception as exc:
        logger.warning("clear synced cookies failed slot=%s: %s", slot_id, exc)
        return 0
    return removed


async def _click_by_texts(page, texts: list[str], *, timeout_ms: int = 25_000) -> str:
    """Click first visible element matching any of the texts (button/link/role)."""
    last_err: Exception | None = None
    for text in texts:
        candidates = [
            lambda t=text: page.get_by_role("button", name=t),
            lambda t=text: page.get_by_role("link", name=t),
            lambda t=text: page.get_by_text(t, exact=True),
            lambda t=text: page.get_by_text(t, exact=False),
        ]
        for make in candidates:
            try:
                loc = make().first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                await loc.click(timeout=timeout_ms)
                return text
            except Exception as exc:
                last_err = exc
                continue
    raise RuntimeError(
        f"Không tìm thấy nút nào trong {texts!r}: {last_err}"
    )


async def run_auto_cycle_for_slot(slot_id: str) -> dict[str, Any]:
    """
    Full cycle for one CDP:
    open → flow URL → clear synced cookies → Create with Google Flow
    → Dự án mới → wait → Sync → close CDP
    """
    from flow2api.services.flow_cdp_auto_settings import get_flow_cdp_auto_settings

    slot = get_flow_cdp_slot(slot_id)
    if not slot:
        return {"ok": False, "error": "slot_not_found"}
    if not str(slot.email or "").strip():
        return {"ok": False, "error": "missing_email", "message": "Slot chưa có email"}

    cfg = get_flow_cdp_auto_settings()
    flow_url = str(cfg.flow_url or _DEFAULT_FLOW_URL).strip() or _DEFAULT_FLOW_URL
    sync_delay = float(cfg.sync_delay_s or 5)

    meta = _running.setdefault(
        slot_id,
        {
            "email": slot.email,
            "role": slot.role,
            "started_at": time.time(),
            "step": "launch",
        },
    )

    try:
        meta["step"] = "launch"
        launch = system_ops.launch_flow_cdp_slot(slot_id, start_url=flow_url)
        if not launch.get("ok"):
            raise RuntimeError(launch.get("message") or launch.get("error") or "launch_failed")

        meta["step"] = "attach"
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("playwright_not_installed") from exc

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(slot.cdp_url())
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()

            meta["step"] = "goto"
            await page.goto(flow_url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(800)

            meta["step"] = "clear_cookies"
            cleared = await _clear_synced_cookies(context, slot_id)
            _log("info", f"{slot_id}: đã clear {cleared} cookies", slot_id=slot_id)
            # Reload so UI shows logged-out / create CTA
            await page.goto(flow_url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(1200)

            meta["step"] = "click_create"
            clicked_create = await _click_by_texts(
                page,
                [
                    "Create with Google Flow",
                    "Tạo bằng Google Flow",
                    "Create with Flow",
                ],
                timeout_ms=35_000,
            )
            _log("info", f"{slot_id}: click «{clicked_create}»", slot_id=slot_id)
            await page.wait_for_timeout(1500)

            meta["step"] = "click_new_project"
            clicked_new = await _click_by_texts(
                page,
                [
                    "Dự án mới",
                    "New project",
                    "New Project",
                ],
                timeout_ms=35_000,
            )
            _log("info", f"{slot_id}: click «{clicked_new}»", slot_id=slot_id)

            meta["step"] = "wait_sync"
            await asyncio.sleep(sync_delay)

            meta["step"] = "sync"
            sync = await sync_session(slot_id)
            if not sync.get("ok"):
                raise RuntimeError(sync.get("message") or sync.get("error") or "sync_failed")
            if not sync.get("email") and not sync.get("token_refreshed"):
                # soft fail if no token — still try close
                raise RuntimeError(sync.get("message") or "sync_incomplete")

            meta["step"] = "close"
            close = system_ops.close_flow_cdp_slot(slot_id)

            # Chờ 10s để Chrome/DB flush cookies & token trước khi apply profile
            meta["step"] = "wait_db"
            await asyncio.sleep(10.0)

            job_cfg = None
            if (slot.role or "bridge") != "center":
                job_cfg = apply_job_parallel_for_profile(slot.profile_id())

            result = {
                "ok": True,
                "slot_id": slot_id,
                "email": sync.get("email") or slot.email,
                "cleared_cookies": cleared,
                "clicked_create": clicked_create,
                "clicked_new": clicked_new,
                "sync": {
                    "email": sync.get("email"),
                    "token_refreshed": sync.get("token_refreshed"),
                    "cookies_count": sync.get("cookies_count"),
                },
                "close": close,
                "job_parallel": job_cfg,
                "message": (
                    f"Xong {slot_id} · sync OK · CDP đã đóng"
                    + (
                        f" · Song song={job_cfg.get('max_concurrent')} · Nhận job"
                        if job_cfg and job_cfg.get("ok")
                        else ""
                    )
                ),
            }
            _log("info", result["message"], slot_id=slot_id)
            return result
        finally:
            try:
                await pw.stop()
            except Exception:
                pass
    except Exception as exc:
        _log("error", f"{slot_id}: {exc}", slot_id=slot_id)
        try:
            system_ops.close_flow_cdp_slot(slot_id)
        except Exception:
            pass
        return {"ok": False, "slot_id": slot_id, "error": str(exc)}
    finally:
        _running.pop(slot_id, None)


def _count_running_by_role(role: str) -> int:
    n = 0
    for meta in _running.values():
        if meta.get("role") == role:
            n += 1
    return n


def apply_job_parallel_for_profile(
    profile_id: str,
    *,
    parallel: int | None = None,
    enable_dispatch: bool = True,
) -> dict[str, Any]:
    """Set Song song (max_concurrent). enable_dispatch=True → bật Nhận job + Img/Vid."""
    from flow2api.services.worker_settings import (
        save_profile_limit,
        set_profile_dispatch_enabled,
        set_profile_media_allowed,
        is_profile_forgotten,
        save_worker_settings,
        get_worker_settings,
    )

    pid = str(profile_id or "").strip()
    if not pid:
        return {"ok": False, "error": "missing_profile_id"}
    cfg = get_flow_cdp_auto_settings()
    mc = int(parallel if parallel is not None else cfg.job_parallel or 8)
    mc = max(1, min(30, mc))
    try:
        if is_profile_forgotten(pid):
            ws = get_worker_settings()
            new_forgotten = [x for x in ws.profile_forgotten if x != pid]
            save_worker_settings(profile_forgotten=new_forgotten)
            _log("info", f"Profile {pid}: đã unforget (bỏ khỏi danh sách ẩn)")
        save_profile_limit(pid, mc)
        if enable_dispatch:
            set_profile_dispatch_enabled(pid, True)
            set_profile_media_allowed(pid, image=True, video=True)
        try:
            get_extension_pool = __import__(
                "flow2api.services.extension_pool", fromlist=["get_extension_pool"]
            ).get_extension_pool
            get_extension_pool().hydrate_db_profiles()
        except Exception:
            pass
        if enable_dispatch:
            _log("info", f"Profile {pid}: Song song={mc} · Nhận job ON · Image/Video ON")
        else:
            _log("info", f"Profile {pid}: Song song={mc} (giữ trạng thái nhận job)")
        return {
            "ok": True,
            "profile_id": pid,
            "max_concurrent": mc,
            "dispatch_enabled": enable_dispatch,
            "image_allowed": enable_dispatch,
            "video_allowed": enable_dispatch,
            "unforgotten": True,
        }
    except Exception as exc:
        _log("error", f"Apply job parallel failed {pid}: {exc}")
        return {"ok": False, "profile_id": pid, "error": str(exc)}


def mark_cdp_profile_standby(profile_id: str, *, reason: str = "") -> dict[str, Any]:
    """CDP mới / lần đầu nhập → tắt Nhận job (standby). Chỉ auto-run mới bật lại."""
    from flow2api.services.worker_settings import set_profile_dispatch_enabled

    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return {"ok": False, "error": "missing_profile_id"}
    try:
        set_profile_dispatch_enabled(pid, False, source="system")
        note = f" ({reason})" if reason else ""
        _log("info", f"Profile {pid}: Nhận job OFF · standby{note}")
        try:
            get_extension_pool = __import__(
                "flow2api.services.extension_pool", fromlist=["get_extension_pool"]
            ).get_extension_pool
            get_extension_pool().hydrate_db_profiles()
        except Exception:
            pass
        return {"ok": True, "profile_id": pid, "dispatch_enabled": False}
    except Exception as exc:
        _log("error", f"mark standby failed {pid}: {exc}")
        return {"ok": False, "profile_id": pid, "error": str(exc)}


def apply_job_parallel_to_enabled_slots() -> dict[str, Any]:
    """Chỉ set Song song cho CDP Gen — không bật Nhận job (giữ standby cho lỗi tài khoản)."""
    cfg = get_flow_cdp_auto_settings()
    results = []
    for slot in ordered_auto_slots():
        if not slot.get("enabled"):
            continue
        if (slot.get("role") or "bridge") == "center":
            continue
        pid = slot.get("linked_profile_id") or slot.get("id")
        results.append(
            apply_job_parallel_for_profile(
                str(pid),
                parallel=cfg.job_parallel,
                enable_dispatch=False,
            )
        )
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_n > 0 or not results, "applied": ok_n, "results": results}


async def _check_expired_receiving_gens() -> None:
    """Canh Gen đang bật Nhận job mà token đã hết / mất lane.

    Khi token wall-clock hết: get_stored_access_token()=None → direct_lane_ready=False
    → profile không còn được pick job (cảm giác «ngưng nhận») nhưng **không** phát
    exception account-switch → flow standby phía dưới không bao giờ được mở.
    """
    cfg = get_flow_cdp_auto_settings()
    if not cfg.enabled:
        return
    now = time.time()
    for s in ordered_auto_slots():
        if not s.get("enabled"):
            continue
        if (s.get("role") or "bridge") == "center":
            continue
        if not s.get("dispatch_enabled"):
            continue
        sid = str(s.get("id") or "").strip()
        pid = str(s.get("linked_profile_id") or sid).strip()
        if not sid or not pid:
            continue
        if _is_cycle_busy(sid):
            continue
        watch_key = f"expired_watch:{pid}"
        if _fail_cooldown_until.get(watch_key, 0) > now:
            continue
        status = str(s.get("token_status") or "").strip().lower()
        rem = s.get("token_remaining_seconds_real")
        if rem is None:
            rem = s.get("token_remaining_seconds")
        try:
            rem_n = float(rem) if rem is not None else None
        except (TypeError, ValueError):
            rem_n = None
        expired = status in ("expired", "missing", "no-session") or (
            rem_n is not None and rem_n <= 0
        )
        if not expired:
            continue
        # Chống spam: 2 phút / profile
        _fail_cooldown_until[watch_key] = now + 120.0
        _log(
            "info",
            (
                f"token_expired (canh lịch): {sid} ({s.get('email') or pid}) "
                f"status={status or '—'} rem={rem_n if rem_n is not None else '—'} "
                f"→ Ngừng job + mở CDP kế"
            ),
            slot_id=sid,
        )
        try:
            await on_profile_http_block(pid, reason="token_expired")
        except Exception as exc:
            _log("error", f"token_expired watch {sid}: {exc}", slot_id=sid)


async def _scheduler_tick() -> None:
    """Scheduler: canh token hết hạn trên Gen đang nhận job → mở CDP kế + bù song song."""
    cfg = get_flow_cdp_auto_settings()
    if not cfg.enabled:
        return
    now = time.time()
    for sid, until in list(_fail_cooldown_until.items()):
        if until <= now:
            _fail_cooldown_until.pop(sid, None)
    await _check_expired_receiving_gens()
    # Bù số Gen nếu < Song song Gen CDP (token chết lặng / restart / miss fill)
    try:
        ensure_gen_slots_for_parallel(reason="scheduler_tick")
    except Exception as exc:
        logger.debug("scheduler ensure_gen_slots: %s", exc)


async def _scheduler_loop() -> None:
    _log(
        "info",
        (
            "Scheduler auto CDP đã chạy "
            "(token hết hạn / lỗi TK → mở CDP kế; bù Song song Gen; 403/429 tạm không đổi Gen)"
        ),
    )
    while True:
        try:
            cfg = get_flow_cdp_auto_settings()
            if not cfg.enabled:
                break
            async with _lock:
                await _scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log("error", f"scheduler tick: {exc}")
        await asyncio.sleep(10.0)
    _log("info", "Scheduler auto CDP đã dừng")


def ensure_scheduler() -> None:
    """Bật vòng scheduler (nếu lịch ON) và bù ngay số Gen theo Song song Gen CDP."""
    global _scheduler_task
    cfg = get_flow_cdp_auto_settings()
    if not cfg.enabled:
        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()
        _scheduler_task = None
        return
    if not (_scheduler_task and not _scheduler_task.done()):
        _scheduler_task = asyncio.create_task(_scheduler_loop())
    # Run lại / GET status / startup: đọc Song song Gen CDP → mở cho đủ mục tiêu
    try:
        ensure_gen_slots_for_parallel(reason="ensure_scheduler")
    except Exception as exc:
        logger.debug("ensure_gen_slots on ensure_scheduler: %s", exc)


async def save_settings_and_nudge(**fields: Any) -> dict[str, Any]:
    save_flow_cdp_auto_settings(**fields)
    # Khi đổi Song song / lưu cấu hình → áp ngay lên profile Gen trong danh sách
    try:
        apply_job_parallel_to_enabled_slots()
    except Exception as exc:
        _log("error", f"apply job_parallel on save: {exc}")
    fill: dict[str, Any] = {}
    try:
        # Số Gen đang hoạt động < Song song Gen CDP → mở CDP tiếp theo cho đủ
        fill = ensure_gen_slots_for_parallel(reason="save_settings")
    except Exception as exc:
        _log("error", f"ensure gen slots on save: {exc}")
        fill = {"ok": False, "error": str(exc)}
    ensure_scheduler()
    status = auto_status()
    if fill:
        status["fill_parallel"] = fill
    return status


async def set_enabled(enabled: bool) -> dict[str, Any]:
    save_flow_cdp_auto_settings(enabled=bool(enabled))
    fill: dict[str, Any] = {}
    if enabled:
        _fail_cooldown_until.clear()
        try:
            apply_job_parallel_to_enabled_slots()
        except Exception as exc:
            _log("error", f"apply job_parallel on enable: {exc}")
        try:
            fill = ensure_gen_slots_for_parallel(reason="enable_auto")
        except Exception as exc:
            _log("error", f"ensure gen slots on enable: {exc}")
            fill = {"ok": False, "error": str(exc)}
        _log(
            "info",
            "Đã BẬT lịch auto CDP (lỗi tài khoản → mở CDP Gen kế tiếp; 403/429 tạm không đổi Gen)",
        )
    else:
        _log("info", "Đã TẮT lịch auto CDP")
    ensure_scheduler()
    status = auto_status()
    if fill:
        status["fill_parallel"] = fill
    return status


async def run_one_now(slot_id: str) -> dict[str, Any]:
    sid = str(slot_id or "").strip()
    if not sid:
        return {"ok": False, "error": "missing_slot_id"}
    if _is_cycle_busy(sid):
        return {"ok": False, "error": "already_running", "message": f"{sid} đang chạy"}
    result = await run_auto_cycle_for_slot(sid)
    return {**result, "status": auto_status()}


async def run_next_now() -> dict[str, Any]:
    """Chạy full cycle cho CDP Gen tiếp theo trên danh sách (cookies → nhận job)."""
    nxt = find_next_cdp_to_run()
    if not nxt:
        return {
            "ok": False,
            "error": "no_next_cdp",
            "message": (
                "Không còn CDP Gen chưa nhận job trong danh sách "
                "(hoặc tất cả đang chạy / đã tắt)"
            ),
            "status": auto_status(),
        }
    sid = str(nxt.get("id") or "").strip()
    email = str(nxt.get("email") or "").strip()
    if _is_cycle_busy(sid):
        return {
            "ok": False,
            "error": "already_running",
            "slot_id": sid,
            "message": f"{sid} đang chạy",
            "status": auto_status(),
        }
    _log(
        "info",
        f"Chạy CDP tiếp theo: {sid}" + (f" ({email})" if email else ""),
        slot_id=sid,
    )
    result = await run_auto_cycle_for_slot(sid)
    msg = result.get("message") or (
        f"Đã chạy {sid}" + (f" · {email}" if email else "")
        if result.get("ok")
        else (result.get("error") or "run_failed")
    )
    return {
        **result,
        "slot_id": sid,
        "email": email or result.get("email"),
        "message": msg,
        "status": auto_status(),
    }
