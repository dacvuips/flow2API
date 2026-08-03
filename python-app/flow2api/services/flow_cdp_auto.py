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
    return {
        "token_remaining_seconds": rem,
        "token_hours_left": meta.get("token_hours_left"),
        "token_status": meta.get("token_status"),
        "access_token_expires_at": meta.get("access_token_expires_at"),
        "dispatch_enabled": dispatch,
        "accepting_jobs": accepting,
        # Standby = có trong danh sách auto nhưng đang ngưng nhận job → sẵn sàng được mở khi 403/429
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
    standby = [s for s in slots if s.get("enabled") and s.get("standby")]
    return {
        "ok": True,
        "settings": cfg.to_dict(),
        "slots": slots,
        "running": running,
        "running_count": len(_running),
        "need_refresh_count": 0,
        "accepting_count": len(accepting),
        "standby_count": len(standby),
        "logs": list(_logs[-40:]),
        "scheduler_alive": bool(_scheduler_task and not _scheduler_task.done()),
        "hint": (
            "Nút «Chạy CDP tiếp theo»: lấy CDP Gen đầu tiên chưa nhận job trên danh sách "
            "→ Sync cookies → bật Nhận job. "
            "Khi lịch bật và gen gặp 403/429 → Ngừng job profile lỗi → mở CDP Gen "
            "tiếp theo trên danh sách (ngay dưới các profile đang nhận job)."
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


def find_next_standby_gen_slot(failed_profile_id: str) -> dict[str, Any] | None:
    """CDP Gen tiếp theo trên danh sách auto: ngay dưới các profile đang nhận job.

    Duyệt theo slot_order, bỏ qua profile vừa lỗi, lấy Gen đầu tiên chưa nhận job
    (standby / ngưng dispatch) — tức phần tử kế tiếp trong danh sách.
    """
    slots = ordered_auto_slots()
    exclude = str(failed_profile_id or "").strip()
    for s in slots:
        if not s.get("enabled"):
            continue
        if (s.get("role") or "bridge") == "center":
            continue
        sid = str(s.get("id") or "").strip()
        linked = str(s.get("linked_profile_id") or sid).strip()
        if exclude and (linked == exclude or sid == exclude):
            continue
        # Đang nhận job → bỏ qua, lấy cái kế tiếp trên danh sách
        if s.get("accepting_jobs"):
            continue
        # Chưa nhận job → mở Sync + Nhận job (tiếp theo danh sách)
        return s
    return None


def find_next_cdp_to_run() -> dict[str, Any] | None:
    """CDP Gen tiếp theo trên danh sách cần chạy full cycle (chưa nhận job / chưa ready)."""
    slots = ordered_auto_slots()
    for s in slots:
        if not s.get("enabled"):
            continue
        if (s.get("role") or "bridge") == "center":
            continue
        sid = str(s.get("id") or "").strip()
        if not sid or sid in _running:
            continue
        # Đã nhận job rồi → bỏ qua, lấy cái kế tiếp
        if s.get("accepting_jobs"):
            continue
        return s
    return None


def _schedule_cycle(slot_id: str, *, reason: str = "") -> bool:
    """Start run_auto_cycle_for_slot in background if not already running."""
    sid = str(slot_id or "").strip()
    if not sid or sid in _running:
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
        if _count_running_by_role("bridge") >= int(cfg.parallel_gen):
            return False

    async def _job(slot_id: str = sid) -> None:
        result = await run_auto_cycle_for_slot(slot_id)
        if result.get("ok"):
            _fail_cooldown_until.pop(slot_id, None)
            _log("info", f"Sync xong {slot_id} (trigger: {reason or 'manual'})", slot_id=slot_id)
        else:
            _fail_cooldown_until[slot_id] = time.time() + _FAIL_COOLDOWN_S

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(_job())
    return True


async def on_profile_http_block(
    failed_profile_id: str,
    *,
    reason: str = "HTTP_403",
) -> dict[str, Any]:
    """
    Khi gen gặp 403/429:
    - Ngừng nhận job profile lỗi
    - Mở CDP Gen tiếp theo trên danh sách (ngay dưới các profile đang nhận job)
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

        set_profile_dispatch_enabled(pid, False)
        _log("info", f"{reason}: đã Ngừng job profile {pid}")
    except Exception as exc:
        _log("error", f"Ngừng job {pid} thất bại: {exc}")

    next_slot = find_next_standby_gen_slot(pid)
    if not next_slot:
        _log(
            "info",
            f"{reason} từ {pid}: không còn CDP Gen standby (ngưng nhận job) phía dưới",
        )
        return {
            "ok": False,
            "failed_profile_id": pid,
            "reason": reason,
            "error": "no_standby_cdp_below",
        }

    next_id = str(next_slot.get("id") or "")
    now = time.time()
    if _fail_cooldown_until.get(next_id, 0) > now:
        return {
            "ok": False,
            "failed_profile_id": pid,
            "next_slot_id": next_id,
            "error": "next_slot_cooldown",
        }
    if next_id in _running:
        return {
            "ok": True,
            "failed_profile_id": pid,
            "next_slot_id": next_id,
            "already_running": True,
        }

    _log(
        "info",
        (
            f"{reason} từ {pid} → mở CDP kế tiếp {next_id} "
            f"({next_slot.get('email')}) · Sync + Nhận job"
        ),
        slot_id=next_id,
    )
    started = _schedule_cycle(next_id, reason=f"{reason}:{pid}")
    return {
        "ok": started,
        "failed_profile_id": pid,
        "next_slot_id": next_id,
        "next_email": next_slot.get("email"),
        "reason": reason,
        "started": started,
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
        set_profile_dispatch_enabled(pid, False)
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
    """Chỉ set Song song cho CDP Gen — không bật Nhận job (giữ standby cho 403/429)."""
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


async def _scheduler_tick() -> None:
    """Scheduler giữ alive khi auto bật — refresh CDP do 403/429 trigger, không canh Active."""
    cfg = get_flow_cdp_auto_settings()
    if not cfg.enabled:
        return
    now = time.time()
    for sid, until in list(_fail_cooldown_until.items()):
        if until <= now:
            _fail_cooldown_until.pop(sid, None)
    # Không còn auto-open theo Active. Trigger qua on_profile_http_block (403/429).


async def _scheduler_loop() -> None:
    _log("info", "Scheduler auto CDP đã chạy (403/429 → mở CDP kế tiếp)")
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
    global _scheduler_task
    cfg = get_flow_cdp_auto_settings()
    if not cfg.enabled:
        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()
        _scheduler_task = None
        return
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())


async def save_settings_and_nudge(**fields: Any) -> dict[str, Any]:
    save_flow_cdp_auto_settings(**fields)
    # Khi đổi Song song / lưu cấu hình → áp ngay lên profile Gen trong danh sách
    try:
        apply_job_parallel_to_enabled_slots()
    except Exception as exc:
        _log("error", f"apply job_parallel on save: {exc}")
    ensure_scheduler()
    return auto_status()


async def set_enabled(enabled: bool) -> dict[str, Any]:
    save_flow_cdp_auto_settings(enabled=bool(enabled))
    if enabled:
        _fail_cooldown_until.clear()
        try:
            apply_job_parallel_to_enabled_slots()
        except Exception as exc:
            _log("error", f"apply job_parallel on enable: {exc}")
        _log(
            "info",
            "Đã BẬT lịch auto CDP (403/429 → mở CDP Gen kế tiếp đang standby)",
        )
    else:
        _log("info", "Đã TẮT lịch auto CDP")
    ensure_scheduler()
    return auto_status()


async def run_one_now(slot_id: str) -> dict[str, Any]:
    sid = str(slot_id or "").strip()
    if not sid:
        return {"ok": False, "error": "missing_slot_id"}
    if sid in _running:
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
    if sid in _running:
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
