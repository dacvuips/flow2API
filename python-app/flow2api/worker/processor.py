from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from flow2api.config import (
    IMAGE_POLL_MAX,
    POLICY_REJECTION_ERROR_MSG,
    POLL_INTERVAL_S,
    RECAPTCHA_RETRY_MAX,
    TASK_RUNNING_TIMEOUT_S,
    TASK_TIMEOUT_ERROR,
    TASK_TIMEOUT_ERROR_MSG,
    TASK_TIMEOUT_RETRY_GRACE_S,
    TASK_TIMEOUT_RETRY_MAX,
    VIDEO_POLL_INTERVAL_S,
    VIDEO_POLL_MAX,
    VIDEO_POLL_MEDIA_MAX,
    WORKER_NUDGE_STUCK_S,
)
from flow2api.services.worker_settings import get_worker_settings
from flow2api.services import activity, flow_sdk
from flow2api.services.api_trace import begin_api_trace, end_api_trace
from flow2api.services.flow_sdk import (
    FlowApiError,
    GetMedia404Error,
    format_api_error,
    is_curl_transport_error,
    is_extension_disconnect_error,
    is_extension_timeout_error,
    is_http_403_failure,
    is_http_429_failure,
    is_gateway_timeout_failure,
    is_policy_rejection_failure,
    is_profile_account_switch_failure,
    is_prominent_people_filter_failure,
    is_trpc_401_failure,
    is_upload_image_internal_failure,
)
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.request_logs import append_request_log
from flow2api.services.request_params import get_video_quality, normalize_request_params
from flow2api.services.result_media import prepare_params_for_worker_requeue
from flow2api.services.stored_media import persist_task_result
from flow2api.services.flow_client import (
    apply_retry_profile_rotation,
    bind_task_profile,
    get_flow_client,
    pick_profile_for_retry,
    pick_profile_for_task,
    profile_available_for_queue,
    request_requires_credit_profile,
    unbind_task_profile,
)
logger = logging.getLogger(__name__)

_IMAGE_VIDEO_TYPES = frozenset(
    {"gen_image_video", "gen_video", "gen_video_start_end", "gen_multi_image_video"}
)


def _task_prompt(row: Any, params: dict[str, Any]) -> str:
    return str(params.get("prompt") or getattr(row, "prompt", "") or "")


def _resolve_video_mode(req_type: str, params: dict[str, Any]) -> str:
    """frame = startImage[/endImage]; component = referenceImages."""
    explicit = str(params.get("video_mode") or "").strip().lower()
    if explicit in ("frame", "component"):
        return explicit
    legacy = {
        "gen_video": "frame",
        "gen_video_start_end": "frame",
        "gen_multi_image_video": "component",
    }
    if req_type == "gen_image_video":
        raise RuntimeError("missing_video_mode")
    return legacy.get(req_type, "")


def _variant_count(params: dict[str, Any]) -> int:
    return max(1, min(int(params.get("variant_count") or 1), 4))


class RequestCancelled(RuntimeError):
    """Raised when user stops a queued/running request."""


class WorkerController:
    def __init__(self) -> None:
        self._scheduler_task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._running_since: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._project_by_profile: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._last_start_monotonic: float = 0.0

    def running_count(self) -> int:
        self._prune_running()
        return len(self._running)

    def _prune_running(self) -> None:
        done = [rid for rid, task in self._running.items() if task.done()]
        for rid in done:
            task = self._running.pop(rid, None)
            self._running_since.pop(rid, None)
            self._cancelled.discard(rid)
            # Safety net: task crashed before marking failed/queued → đừng để treo running 20p
            if task is None:
                continue
            try:
                exc = task.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                exc = None
            if not exc:
                continue
            row = activity.get_request(rid)
            if not row or row.status != "running":
                continue
            msg = format_api_error(exc).strip() or type(exc).__name__
            logger.error("running task died uncleanly rid=%s err=%s", rid[:8], msg)
            activity.update_request(
                rid,
                status=f"failed: {msg}",
                error=msg,
                result={"error": msg},
            )
            append_request_log(rid, "worker", f"Job aborted: {msg}", level="error")
            events.publish("request_finished", {"id": rid, "status": "failed"})

    def _requeue_no_profile(self, rid: str, params: dict[str, Any], *, reason: str) -> None:
        """Đưa task về queued khi không gán được profile (tránh treo running → timeout 20m)."""
        params = dict(params or {})
        params.pop("running_started_at", None)
        # Chờ ngắn rồi scheduler thử lại khi có slot profile
        params["retry_not_before"] = time.time() + 2.0
        activity.update_request(rid, status="queued", params=params, error=None)
        append_request_log(
            rid,
            "worker",
            f"Chưa gán được profile ({reason}) — về hàng đợi",
            level="warn",
        )
        events.publish("request_finished", {"id": rid, "status": "queued"})
        logger.warning("requeue no profile rid=%s reason=%s", rid[:8], reason)

    def request_cancel(self, rid: str) -> None:
        self._cancelled.add(rid)

    def cancel_running_tasks(self, ids: set[str] | None = None) -> None:
        """Cancel in-flight asyncio tasks (optional subset by request id).

        Before cancelling the asyncio task we snapshot the ws req_ids that
        are currently in-flight on any extension for that task and fire a
        best-effort `abort_request` message so the Chrome extension can
        tear the underlying `fetch()` down. The snapshot has to happen
        BEFORE ``task.cancel()`` because ``_send``'s finally-block cleanup
        would otherwise erase the mapping we need.
        """
        self._prune_running()
        targets = set(ids) if ids is not None else set(self._running.keys())
        pool = get_extension_pool()

        abort_snapshot: list = []
        for rid in list(self._running.keys()):
            if rid not in targets:
                continue
            abort_snapshot.extend(pool.snapshot_trace_pending(rid))

        # Fire-and-forget so cancel_running_tasks stays sync and callers
        # (including sync internal ones like _handle_running_stuck) don't
        # have to await. If we're not inside a running loop, silently
        # drop the abort — the asyncio-task cancel below still runs.
        if abort_snapshot:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(pool.abort_snapshot(abort_snapshot))
            except RuntimeError:
                pass

        for rid in list(self._running.keys()):
            if rid not in targets:
                continue
            task = self._running.get(rid)
            if task and not task.done():
                task.cancel()

    def prepare_retry(self, rid: str) -> None:
        """Stop in-flight work so the same request id can run again."""
        self._prune_running()
        task = self._running.get(rid)
        self._running_since.pop(rid, None)
        if task and not task.done():
            self.request_cancel(rid)
            task.cancel()
        else:
            self._cancelled.discard(rid)
            self._running.pop(rid, None)

    def _handle_running_stuck(self, rid: str) -> None:
        """Fail or auto-retry when running exceeds TASK_RUNNING_TIMEOUT_S (default 20m).

        On task_timeout_20m: retry once if not already retried and age since created_at
        ≤ running_timeout + grace (default 20m + 10m). Otherwise mark failed.
        """
        row = activity.get_request(rid)
        if not row or row.status != "running":
            return

        task = self._running.get(rid)
        self._running_since.pop(rid, None)

        params: dict[str, Any] = {}
        try:
            raw = json.loads(row.params_json or "{}")
            if isinstance(raw, dict):
                params = raw
        except Exception:
            params = {}

        retry_count = int(params.get("running_timeout_retry_count") or 0)
        created = row.created_at or datetime.utcnow()
        try:
            age_s = max(0.0, (datetime.utcnow() - created).total_seconds())
        except Exception:
            age_s = float("inf")

        age_limit_s = max(60, int(TASK_RUNNING_TIMEOUT_S or 1200)) + max(
            0, int(TASK_TIMEOUT_RETRY_GRACE_S or 600)
        )
        can_retry = retry_count < max(1, int(TASK_TIMEOUT_RETRY_MAX or 1)) and age_s <= age_limit_s
        if can_retry:
            params["running_timeout_retry_count"] = retry_count + 1
            params["retry_not_before"] = time.time() + 2.0
            params.pop("running_started_at", None)
            activity.update_request(rid, status="queued", params=params, error=None)
            append_request_log(
                rid,
                "worker",
                (
                    f"{TASK_TIMEOUT_ERROR} — auto retry {retry_count + 1}/"
                    f"{max(1, int(TASK_TIMEOUT_RETRY_MAX or 1))} "
                    f"(age {age_s:.0f}s ≤ {age_limit_s}s, chưa báo lỗi)"
                ),
                level="warn",
            )
            events.publish("request_finished", {"id": rid, "status": "queued"})
            logger.warning(
                "task timeout → retry rid=%s age=%.0fs retry=%s",
                rid[:8],
                age_s,
                retry_count + 1,
            )
            self.request_cancel(rid)
            if task and not task.done():
                task.cancel()
            self._running.pop(rid, None)
            return

        activity.update_request(
            rid,
            status=f"failed: {TASK_TIMEOUT_ERROR}",
            error=TASK_TIMEOUT_ERROR_MSG,
            result={"error": TASK_TIMEOUT_ERROR_MSG},
        )
        append_request_log(
            rid,
            "worker",
            TASK_TIMEOUT_ERROR_MSG,
            level="warn",
        )
        events.publish("request_finished", {"id": rid, "status": "failed"})
        logger.warning(
            "task timed out rid=%s after %ss (age=%.0fs retries=%s)",
            rid[:8],
            TASK_RUNNING_TIMEOUT_S,
            age_s,
            retry_count,
        )
        self.request_cancel(rid)
        if task and not task.done():
            task.cancel()
        self._running.pop(rid, None)

    def _expire_stale_running(
        self,
        *,
        limit_s: int | None = None,
        orphans_only: bool = False,
    ) -> list[str]:
        active_limit = max(60, int(TASK_RUNNING_TIMEOUT_S or 1200))
        orphan_limit = max(60, int(limit_s or active_limit))
        handled: list[str] = []
        if not orphans_only:
            now = time.monotonic()
            for rid, started in list(self._running_since.items()):
                if now - started >= active_limit:
                    self._handle_running_stuck(rid)
                    handled.append(rid)
        for row in activity.list_running_requests():
            if row.id in self._running_since:
                continue
            if activity.running_age_seconds(row) >= orphan_limit:
                self._handle_running_stuck(row.id)
                handled.append(row.id)
        return handled

    def _count_running_by_profile(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rid in self._running:
            row = activity.get_request(rid)
            if not row:
                continue
            params = json.loads(row.params_json or "{}")
            profile_id = str(params.get("profile_id") or "")
            if profile_id:
                counts[profile_id] = counts.get(profile_id, 0) + 1
        return counts

    def scheduler_alive(self) -> bool:
        return self._scheduler_task is not None and not self._scheduler_task.done()

    async def nudge(self) -> dict[str, Any]:
        actions: list[str] = []
        self._prune_running()

        if not self.scheduler_alive():
            if self._scheduler_task and self._scheduler_task.done():
                exc = self._scheduler_task.exception()
                if exc:
                    logger.error("scheduler died: %s", exc, exc_info=exc)
            await self.start()
            actions.append("scheduler_restarted")

        pool = get_extension_pool()
        pool.hydrate_db_profiles()
        drift = pool.reconcile_active_jobs(self._count_running_by_profile())
        if drift:
            actions.append("active_jobs_reconciled")
            logger.warning("active_jobs drift fixed: %s", drift)

        stuck = self._expire_stale_running(
            limit_s=WORKER_NUDGE_STUCK_S,
            orphans_only=True,
        )
        if stuck:
            actions.append(f"orphan_running_requeued:{len(stuck)}")

        try:
            await pool.broadcast({"type": "nudge"})
        except Exception as exc:
            logger.warning("extension nudge broadcast failed: %s", exc)

        return {
            "ok": True,
            "actions": actions,
            "queued": activity.count_queued(),
            "running_slots": self.running_count(),
            "scheduler_alive": self.scheduler_alive(),
            "active_jobs_drift": drift,
            "stale_handled": stuck,
        }

    def _raise_if_cancelled(self, rid: str) -> None:
        if rid in self._cancelled:
            raise RequestCancelled("canceled")
        row = activity.get_request(rid)
        if row and row.status.startswith("failed:") and "cancel" in row.status.lower():
            raise RequestCancelled("canceled")
        if row and row.status == "queued":
            raise RequestCancelled("canceled")
        if row:
            try:
                params = json.loads(row.params_json or "{}")
            except Exception:
                params = {}
            pid = str(params.get("profile_id") or "").strip()
            if pid:
                from flow2api.services.worker_settings import is_profile_dispatch_enabled

                if not is_profile_dispatch_enabled(pid):
                    raise RuntimeError("PROFILE_DISPATCH_DISABLED")

    def _abort_hook(self, rid: str):
        return lambda: self._raise_if_cancelled(rid)

    def requeue_running_on_profile(
        self,
        profile_id: str,
        *,
        reason: str = "dispatch_disabled",
    ) -> int:
        """Ngưng nhận job → bỏ gán queued/running khỏi profile, chuyển profile đang hoạt động."""
        pid = str(profile_id or "").strip()
        if not pid:
            return 0
        self._prune_running()
        targets: list[str] = []
        for row in activity.list_active_requests():
            if row.status not in ("queued", "running"):
                continue
            try:
                params = normalize_request_params(json.loads(row.params_json or "{}"))
            except Exception:
                continue
            if str(params.get("profile_id") or "").strip() != pid:
                continue
            targets.append(row.id)

        if not targets:
            return 0

        pool = get_extension_pool()
        abort_snapshot: list = []
        for rid in targets:
            abort_snapshot.extend(pool.snapshot_trace_pending(rid))
        if abort_snapshot:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(pool.abort_snapshot(abort_snapshot))
            except RuntimeError:
                pass

        moved = 0
        for rid in targets:
            row = activity.get_request(rid)
            if not row or row.status not in ("queued", "running"):
                continue
            was_running = row.status == "running"
            try:
                params = normalize_request_params(json.loads(row.params_json or "{}"))
            except Exception:
                params = {"profile_id": pid}
            params["retry_exclude_profile_id"] = pid
            params.pop("profile_assigned_by_user", None)
            rotated = apply_retry_profile_rotation(params, row.type)
            if str(rotated.get("profile_id") or "").strip() == pid:
                rotated.pop("profile_id", None)
                rotated.pop("profile_label", None)
                rotated.pop("profile_email", None)
                rotated["retry_exclude_profile_id"] = pid
            rotated.pop("profile_assigned_by_user", None)
            rotated.pop("running_started_at", None)
            activity.update_request(rid, status="queued", params=rotated, error=None)
            append_request_log(
                rid,
                "worker",
                (
                    f"Profile {pid[:12]} ngưng nhận job ({reason}) "
                    "— bỏ gán, chuyển profile đang hoạt động"
                ),
                level="warn",
                profile_id=str(
                    rotated.get("profile_id") or rotated.get("retry_exclude_profile_id") or ""
                )
                or None,
            )
            events.publish("request_finished", {"id": rid, "status": "queued"})
            logger.warning(
                "requeue rid=%s off profile=%s reason=%s → %s",
                rid[:8],
                pid[:12],
                reason,
                str(rotated.get("profile_id") or "-")[:12],
            )
            if was_running:
                task = self._running.get(rid)
                self.request_cancel(rid)
                if task and not task.done():
                    task.cancel()
            moved += 1
        return moved

    def _persist_params(self, rid: str, params: dict[str, Any]) -> None:
        activity.update_request(rid, params=params)

    async def _resolve_start_media_id(
        self,
        rid: str,
        client: Any,
        project_id: str,
        params: dict[str, Any],
    ) -> str:
        start_id = params.get("start_media_id")
        if start_id:
            return str(start_id)
        image_base64s = params.get("image_base64s") or []
        if not image_base64s:
            raise RuntimeError("missing_start_media_id")
        ids = await flow_sdk.upload_images(
            client,
            project_id=project_id,
            image_base64s=image_base64s[:1],
        )
        if not ids:
            raise RuntimeError("upload_start_image_failed")
        start_id = ids[0]
        params["start_media_id"] = start_id
        self._persist_params(rid, params)
        return start_id

    async def _resolve_start_end_media_ids(
        self,
        rid: str,
        client: Any,
        project_id: str,
        params: dict[str, Any],
    ) -> tuple[str, str]:
        start_id = params.get("start_media_id")
        end_id = params.get("end_media_id")
        if start_id and end_id:
            return str(start_id), str(end_id)
        image_base64s = params.get("image_base64s") or []
        if len(image_base64s) < 2:
            raise RuntimeError("missing_start_end_images")
        ids = await flow_sdk.upload_images(
            client,
            project_id=project_id,
            image_base64s=image_base64s[:2],
        )
        if len(ids) < 2:
            raise RuntimeError("upload_start_end_images_failed")
        params["start_media_id"] = ids[0]
        params["end_media_id"] = ids[1]
        self._persist_params(rid, params)
        return ids[0], ids[1]

    def _has_video_input_media(self, params: dict[str, Any]) -> bool:
        if params.get("start_media_id") or params.get("end_media_id"):
            return True
        if params.get("reference_media_ids"):
            return True
        if params.get("video_media_id") or params.get("source_video_media_id"):
            return True
        if params.get("video_media_ids"):
            return True
        if any(bool(x) for x in (params.get("video_base64s") or [])):
            return True
        return any(bool(x) for x in (params.get("image_base64s") or []))

    async def _resolve_omni_video_media_id(
        self,
        rid: str,
        client: Any,
        project_id: str,
        params: dict[str, Any],
    ) -> str:
        video_id = str(
            params.get("video_media_id")
            or params.get("source_video_media_id")
            or ""
        ).strip()
        if video_id:
            return video_id
        video_ids = params.get("video_media_ids") or []
        if video_ids:
            video_id = str(video_ids[0] or "").strip()
            if video_id:
                params["video_media_id"] = video_id
                self._persist_params(rid, params)
                return video_id
        video_b64s = params.get("video_base64s") or []
        if not video_b64s:
            return ""
        uploaded = await flow_sdk.upload_video(
            client,
            project_id=project_id,
            video_base64=str(video_b64s[0]),
        )
        video_id = str(uploaded.get("media_id") or "")
        if not video_id:
            raise RuntimeError("upload_video_failed")
        params["video_media_id"] = video_id
        self._persist_params(rid, params)
        return video_id

    async def _resolve_omni_component_media(
        self,
        rid: str,
        client: Any,
        project_id: str,
        params: dict[str, Any],
    ) -> tuple[list[str], str]:
        video_id = await self._resolve_omni_video_media_id(rid, client, project_id, params)
        max_images = (
            flow_sdk.OMNI_COMPONENT_MAX_IMAGES_WITH_VIDEO
            if video_id
            else flow_sdk.OMNI_COMPONENT_MAX_IMAGES_ONLY
        )
        ref_ids = list(params.get("reference_media_ids") or [])
        if not ref_ids:
            image_base64s = params.get("image_base64s") or []
            if image_base64s:
                refs = image_base64s[:max_images]
                ref_ids = await flow_sdk.upload_images(
                    client,
                    project_id=project_id,
                    image_base64s=refs,
                    max_concurrent=flow_sdk.UPLOAD_IMAGES_MAX_CONCURRENT,
                )
        if ref_ids:
            params["reference_media_ids"] = ref_ids
            self._persist_params(rid, params)
        if not ref_ids and not video_id:
            raise RuntimeError("missing_omni_component_media")
        return [str(x) for x in ref_ids], video_id

    async def _resolve_reference_media_ids(
        self,
        rid: str,
        client: Any,
        project_id: str,
        params: dict[str, Any],
    ) -> list[str]:
        ref_ids = list(params.get("reference_media_ids") or [])
        if ref_ids:
            return [str(x) for x in ref_ids]
        image_base64s = params.get("image_base64s") or []
        if not image_base64s:
            raise RuntimeError("missing_reference_images")
        imgs = image_base64s[:3]
        ref_ids = await flow_sdk.upload_images(
            client,
            project_id=project_id,
            image_base64s=imgs,
        )
        if not ref_ids:
            raise RuntimeError("missing_reference_images")
        params["reference_media_ids"] = ref_ids
        self._persist_params(rid, params)
        return ref_ids

    async def start(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._stop.clear()
        self._expire_stale_running()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._scheduler_task:
            await self._scheduler_task
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    async def _ensure_project(self, profile_id: str) -> str:
        if profile_id in self._project_by_profile:
            return self._project_by_profile[profile_id]
        client = get_flow_client()
        project_id = await flow_sdk.ensure_project(client)
        self._project_by_profile[profile_id] = project_id
        return project_id

    def _assign_profile(
        self,
        rid: str,
        params: dict[str, Any],
        request_type: str | None = None,
    ) -> str:
        credit_required = request_requires_credit_profile(params, request_type)
        existing = params.get("profile_id")
        exclude = params.get("retry_exclude_profile_id")
        if params.get("profile_assigned_by_user") and existing:
            profile_id = pick_profile_for_task(
                str(existing),
                credit_required=credit_required,
                request_type=request_type,
            )
            if not profile_id:
                params.pop("profile_assigned_by_user", None)
                profile_id = pick_profile_for_task(
                    None,
                    credit_required=credit_required,
                    request_type=request_type,
                )
        elif existing:
            profile_id = pick_profile_for_task(
                str(existing),
                credit_required=credit_required,
                request_type=request_type,
            )
            if not profile_id:
                profile_id = pick_profile_for_task(
                    None,
                    credit_required=credit_required,
                    request_type=request_type,
                )
        elif exclude:
            profile_id = pick_profile_for_retry(
                str(exclude),
                credit_required=credit_required,
                request_type=request_type,
            )
        else:
            profile_id = pick_profile_for_task(
                None,
                credit_required=credit_required,
                request_type=request_type,
            )
        if not profile_id:
            raise RuntimeError("no_extension_profile_online")
        session = get_extension_pool().get(profile_id)
        params["profile_id"] = profile_id
        params.pop("retry_exclude_profile_id", None)
        if session:
            params["profile_label"] = session.display_name()
            if session.email:
                params["profile_email"] = session.email
            else:
                params.pop("profile_email", None)
        self._persist_params(rid, params)
        # Reserve slot sau persist — tránh leak nếu ghi params lỗi; chặn race vượt limit
        if not get_extension_pool().try_reserve_job(profile_id):
            raise RuntimeError("no_extension_profile_online")
        return profile_id

    def _requeue_for_retry(
        self,
        rid: str,
        params: dict[str, Any],
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        row = activity.get_request(rid)
        params = apply_retry_profile_rotation(params, row.type if row else None)
        params.pop("running_started_at", None)
        activity.update_request(rid, status="queued", params=params, error=None)
        events.publish("request_finished", {"id": rid, "status": "queued"})
        return params

    def _handle_profile_error_switch(
        self,
        rid: str,
        retry_params: dict[str, Any],
        msg: str,
        *,
        label: str = "HTTP 403",
        reset_recaptcha_retries: bool = False,
    ) -> None:
        failed_profile = str(retry_params.get("profile_id") or "").strip()
        if reset_recaptcha_retries:
            retry_params.pop("recaptcha_retry_count", None)
        retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
        append_request_log(
            rid,
            "worker",
            (
                f"{label} — chuyển profile khác ngay"
                + (f" (từ {failed_profile[:12]})" if failed_profile else "")
            ),
            level="warn",
            profile_id=failed_profile or None,
        )
        logger.warning(
            "%s → requeue rid=%s profile=%s",
            label,
            rid[:8],
            failed_profile[:12] or "-",
        )
        # Auto CDP: chỉ lỗi tài khoản thật (auth hết hạn / quota) → mở Gen kế tiếp.
        # label đã được class account-switch ở caller → không yêu cầu marker trong msg
        # (tránh miss khi marker chỉ có trong api_trace/exc).
        _cdp_switch_labels = {
            "account_error",
            "offline_auth_expired",
            "token_expired",
            "invalid_auth",
            "quota_exhausted",
        }
        if failed_profile and (
            label in _cdp_switch_labels
            or is_profile_account_switch_failure(None, msg)
        ):
            self._trigger_next_cdp_on_block(failed_profile, label)

    def _trigger_next_cdp_on_block(self, failed_profile_id: str, reason: str) -> None:
        try:
            from flow2api.services.flow_cdp_auto import on_profile_http_block

            loop = asyncio.get_running_loop()
            loop.create_task(
                on_profile_http_block(failed_profile_id, reason=reason)
            )
        except RuntimeError:
            logger.debug("no running loop for CDP auto trigger")
        except Exception as exc:
            logger.warning("trigger next CDP failed: %s", exc)

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                started = await self._scheduler_tick()
            except Exception:
                logger.exception("scheduler tick failed")
                await asyncio.sleep(1)
                continue
            if started == 0 and not self._running:
                await asyncio.sleep(POLL_INTERVAL_S)
            else:
                await asyncio.sleep(0.25)

    async def _scheduler_tick(self) -> int:
        self._prune_running()
        self._expire_stale_running()
        settings = get_worker_settings()
        started = 0
        pool = get_extension_pool()
        if pool.ready_count() == 0:
            # Hydrate DB profiles mỗi tick khi pool rỗng (bắt direct-lane offline gen)
            pool.hydrate_db_profiles()
            if pool.ready_count() == 0:
                return 0
        # Không dùng giới hạn Tổng global — capacity = tổng slot các profile đang nhận job
        slots = pool.available_job_slots()
        if slots <= 0 or not pool.has_available_profile():
            return 0
        rows = activity.next_queued_batch(max(slots * 2, slots))
        started_this_round = 0
        for row in rows:
            if started_this_round >= slots:
                break
            if row.id in self._running:
                continue
            row_params = normalize_request_params(json.loads(row.params_json or "{}"))
            retry_not_before = float(row_params.get("retry_not_before") or 0)
            if retry_not_before > time.time():
                continue
            if not profile_available_for_queue(row_params, row.type):
                continue
            stagger = settings.task_stagger_s
            if stagger > 0 and self._last_start_monotonic > 0:
                wait_s = stagger - (time.monotonic() - self._last_start_monotonic)
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
            row_params.pop("retry_not_before", None)
            row_params["running_started_at"] = datetime.utcnow().isoformat() + "Z"
            activity.update_request(row.id, status="running", params=row_params)
            events.publish("request_started", {"id": row.id})
            self._running_since[row.id] = time.monotonic()
            self._running[row.id] = asyncio.create_task(self._run_job(row.id))
            self._last_start_monotonic = time.monotonic()
            started += 1
            started_this_round += 1
        return started

    async def _run_job(self, rid: str) -> None:
        profile_id = ""
        pool = get_extension_pool()
        client = None
        began_trace = False
        try:
            row = activity.get_request(rid)
            if not row:
                return
            if row.status != "running":
                return
            params = normalize_request_params(json.loads(row.params_json or "{}"))
            try:
                profile_id = self._assign_profile(rid, params, row.type)
            except Exception as exc:
                reason = format_api_error(exc).strip() or type(exc).__name__
                self._requeue_no_profile(rid, params, reason=reason)
                return

            bind_task_profile(profile_id)
            client = get_flow_client()
            client.trace_request_id = rid
            begin_api_trace(rid)
            began_trace = True
            append_request_log(
                rid,
                "worker",
                f"Job started type={row.type} model={row.model or '-'}",
                level="info",
                data={"profile_id": profile_id},
            )
            try:
                self._raise_if_cancelled(rid)
                await self._process_one(rid)
            except RequestCancelled:
                end_api_trace(rid)
                began_trace = False
                cur = activity.get_request(rid)
                if cur and cur.status == "queued":
                    raise
                append_request_log(rid, "worker", "Job canceled", level="warn")
                if cur and cur.status == "running":
                    activity.update_request(
                        rid,
                        status="failed: canceled",
                        error="canceled",
                        result={"error": "canceled"},
                    )
                events.publish("request_finished", {"id": rid, "status": "canceled"})
            except asyncio.CancelledError:
                end_api_trace(rid)
                began_trace = False
                cur = activity.get_request(rid)
                if cur and (
                    cur.status == "queued" or cur.status.startswith("failed:")
                ):
                    raise
                if cur and cur.status == "running":
                    activity.update_request(
                        rid,
                        status=f"failed: {TASK_TIMEOUT_ERROR}",
                        error=TASK_TIMEOUT_ERROR_MSG,
                        result={"error": TASK_TIMEOUT_ERROR_MSG},
                    )
                    append_request_log(
                        rid,
                        "worker",
                        TASK_TIMEOUT_ERROR_MSG,
                        level="warn",
                    )
                    events.publish("request_finished", {"id": rid, "status": "failed"})
                raise
            except Exception as exc:
                cur = activity.get_request(rid)
                if cur and (
                    cur.status.startswith("failed:")
                    or cur.error in (TASK_TIMEOUT_ERROR, TASK_TIMEOUT_ERROR_MSG, "canceled")
                ):
                    return
                api_trace = end_api_trace(rid)
                began_trace = False
                msg = format_api_error(exc).strip() or "unknown_error"
                cur = activity.get_request(rid)
                retry_params = json.loads(cur.params_json or "{}") if cur else {}
                recaptcha_retry = int(retry_params.get("recaptcha_retry_count") or 0)
                if flow_sdk.is_recaptcha_error(msg) and recaptcha_retry < RECAPTCHA_RETRY_MAX:
                    delay_s = flow_sdk.recaptcha_retry_delay(recaptcha_retry)
                    retry_params["recaptcha_retry_count"] = recaptcha_retry + 1
                    retry_params["retry_not_before"] = time.time() + delay_s
                    retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                    append_request_log(
                        rid,
                        "worker",
                        (
                            f"reCAPTCHA retry {recaptcha_retry + 1}/{RECAPTCHA_RETRY_MAX} "
                            f"— chờ {delay_s:.1f}s"
                        ),
                        level="warn",
                        profile_id=str(
                            retry_params.get("profile_id")
                            or retry_params.get("retry_exclude_profile_id")
                            or ""
                        )
                        or None,
                        profile_email=str(retry_params.get("profile_email") or "") or None,
                    )
                    return
                if flow_sdk.is_recaptcha_error(msg):
                    self._handle_profile_error_switch(
                        rid,
                        retry_params,
                        msg,
                        label="reCAPTCHA 403",
                        reset_recaptcha_retries=True,
                    )
                    return
                if is_policy_rejection_failure(exc, msg, api_trace):
                    msg = POLICY_REJECTION_ERROR_MSG
                upload_internal_retry = int(retry_params.get("upload_internal_retry_count") or 0)
                if (
                    is_upload_image_internal_failure(exc, msg, api_trace)
                    and upload_internal_retry < RECAPTCHA_RETRY_MAX
                ):
                    delay_s = flow_sdk.recaptcha_retry_delay(upload_internal_retry)
                    retry_params["upload_internal_retry_count"] = upload_internal_retry + 1
                    retry_params["retry_not_before"] = time.time() + delay_s
                    retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                    logger.warning(
                        "upload_image internal error retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                        upload_internal_retry + 1,
                        RECAPTCHA_RETRY_MAX,
                        rid[:8],
                        delay_s,
                        str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                    )
                    return
                extension_timeout_retry = int(retry_params.get("extension_timeout_retry_count") or 0)
                if (
                    is_extension_timeout_error(msg, exc)
                    and extension_timeout_retry < RECAPTCHA_RETRY_MAX
                ):
                    delay_s = flow_sdk.recaptcha_retry_delay(extension_timeout_retry)
                    retry_params["extension_timeout_retry_count"] = extension_timeout_retry + 1
                    retry_params["retry_not_before"] = time.time() + delay_s
                    retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                    logger.warning(
                        "extension_timeout retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                        extension_timeout_retry + 1,
                        RECAPTCHA_RETRY_MAX,
                        rid[:8],
                        delay_s,
                        str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                    )
                    return
                curl_transport_retry = int(retry_params.get("curl_transport_retry_count") or 0)
                if (
                    is_curl_transport_error(msg, exc)
                    and curl_transport_retry < RECAPTCHA_RETRY_MAX
                ):
                    delay_s = flow_sdk.recaptcha_retry_delay(curl_transport_retry)
                    retry_params["curl_transport_retry_count"] = curl_transport_retry + 1
                    retry_params["retry_not_before"] = time.time() + delay_s
                    retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                    logger.warning(
                        "curl_transport retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                        curl_transport_retry + 1,
                        RECAPTCHA_RETRY_MAX,
                        rid[:8],
                        delay_s,
                        str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                    )
                    return
                extension_disconnect_retry = int(
                    retry_params.get("extension_disconnect_retry_count") or 0
                )
                if (
                    is_extension_disconnect_error(msg, exc)
                    and extension_disconnect_retry < RECAPTCHA_RETRY_MAX
                ):
                    delay_s = flow_sdk.recaptcha_retry_delay(extension_disconnect_retry)
                    retry_params["extension_disconnect_retry_count"] = extension_disconnect_retry + 1
                    retry_params["retry_not_before"] = time.time() + delay_s
                    retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                    logger.warning(
                        "extension_disconnected retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                        extension_disconnect_retry + 1,
                        RECAPTCHA_RETRY_MAX,
                        rid[:8],
                        delay_s,
                        str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                    )
                    return
                trpc_401_retry = int(retry_params.get("trpc_401_retry_count") or 0)
                if is_trpc_401_failure(exc, msg, api_trace) and trpc_401_retry < RECAPTCHA_RETRY_MAX:
                    delay_s = flow_sdk.recaptcha_retry_delay(trpc_401_retry)
                    retry_params["trpc_401_retry_count"] = trpc_401_retry + 1
                    retry_params["retry_not_before"] = time.time() + delay_s
                    retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                    append_request_log(
                        rid,
                        "worker",
                        (
                            f"TRPC_401 retry {trpc_401_retry + 1}/{RECAPTCHA_RETRY_MAX} "
                            f"— chờ {delay_s:.1f}s"
                        ),
                        level="warn",
                        profile_id=str(
                            retry_params.get("profile_id")
                            or retry_params.get("retry_exclude_profile_id")
                            or ""
                        )
                        or None,
                    )
                    logger.warning(
                        "TRPC_401 retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                        trpc_401_retry + 1,
                        RECAPTCHA_RETRY_MAX,
                        rid[:8],
                        delay_s,
                        str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                    )
                    return
                if "PROFILE_DISPATCH_DISABLED" in msg.upper():
                    failed_profile = str(retry_params.get("profile_id") or "").strip()
                    retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                    append_request_log(
                        rid,
                        "worker",
                        (
                            "Profile đã ngưng nhận job — chuyển profile khác"
                            + (f" (từ {failed_profile[:12]})" if failed_profile else "")
                        ),
                        level="warn",
                        profile_id=failed_profile or None,
                    )
                    logger.warning(
                        "dispatch off → requeue rid=%s profile=%s",
                        rid[:8],
                        failed_profile[:12] or "-",
                    )
                    return
                if is_gateway_timeout_failure(exc, msg, api_trace):
                    # 524/502/504/timeout = Cloudflare/origin — không phải lỗi tài khoản.
                    # Retry cùng profile, không ngừng job / không ẩn profile.
                    http_524_retry = int(retry_params.get("http_524_retry_count") or 0)
                    if http_524_retry < RECAPTCHA_RETRY_MAX:
                        delay_s = flow_sdk.recaptcha_retry_delay(http_524_retry)
                        retry_params["http_524_retry_count"] = http_524_retry + 1
                        retry_params["retry_not_before"] = time.time() + delay_s
                        retry_params.pop("running_started_at", None)
                        activity.update_request(
                            rid, status="queued", params=retry_params, error=None
                        )
                        events.publish(
                            "request_finished", {"id": rid, "status": "queued"}
                        )
                        append_request_log(
                            rid,
                            "worker",
                            (
                                f"HTTP timeout/524 — giữ profile, retry "
                                f"{http_524_retry + 1}/{RECAPTCHA_RETRY_MAX} "
                                f"sau {delay_s:.1f}s"
                            ),
                            level="warn",
                            profile_id=str(retry_params.get("profile_id") or "") or None,
                        )
                        logger.warning(
                            "HTTP timeout/524 retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                            http_524_retry + 1,
                            RECAPTCHA_RETRY_MAX,
                            rid[:8],
                            delay_s,
                            str(retry_params.get("profile_id") or "-")[:12],
                        )
                        return
                if is_profile_account_switch_failure(exc, msg, api_trace):
                    switch_label = "account_error"
                    low = str(msg or "").lower()
                    if "offline_auth_expired" in low:
                        switch_label = "offline_auth_expired"
                    elif "token hết hạn" in low or "token het han" in low or "token expired" in low or "token_expired" in low:
                        switch_label = "token_expired"
                    elif "invalid authentication credentials" in low:
                        switch_label = "invalid_auth"
                    elif "resource has been exhausted" in low:
                        switch_label = "quota_exhausted"
                    self._handle_profile_error_switch(
                        rid,
                        retry_params,
                        msg,
                        label=switch_label,
                    )
                    return
                if is_http_403_failure(exc, msg, api_trace):
                    retry_params.pop("http_403_retry_count", None)
                    self._handle_profile_error_switch(
                        rid,
                        retry_params,
                        msg,
                        label="HTTP 403",
                    )
                    return
                if is_http_429_failure(exc, msg, api_trace):
                    http_429_retry = int(retry_params.get("http_429_retry_count") or 0)
                    if http_429_retry < RECAPTCHA_RETRY_MAX:
                        delay_s = flow_sdk.recaptcha_retry_delay(http_429_retry)
                        retry_params["http_429_retry_count"] = http_429_retry + 1
                        retry_params["retry_not_before"] = time.time() + delay_s
                        retry_params.pop("running_started_at", None)
                        activity.update_request(
                            rid, status="queued", params=retry_params, error=None
                        )
                        events.publish(
                            "request_finished", {"id": rid, "status": "queued"}
                        )
                        append_request_log(
                            rid,
                            "worker",
                            (
                                f"HTTP 429 tạm thời — giữ profile, retry "
                                f"{http_429_retry + 1}/{RECAPTCHA_RETRY_MAX} "
                                f"sau {delay_s:.1f}s"
                            ),
                            level="warn",
                            profile_id=str(retry_params.get("profile_id") or "") or None,
                        )
                        logger.warning(
                            "HTTP 429 transient retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                            http_429_retry + 1,
                            RECAPTCHA_RETRY_MAX,
                            rid[:8],
                            delay_s,
                            str(retry_params.get("profile_id") or "-")[:12],
                        )
                        return
                if isinstance(exc, FlowApiError):
                    logger.error(
                        "worker failed rid=%s step=%s err=%s api_trace=%s",
                        rid,
                        exc.step,
                        msg,
                        len(api_trace),
                    )
                else:
                    logger.exception("worker failed rid=%s api_trace=%s", rid, len(api_trace))
                display_msg = flow_sdk.sanitize_public_error(
                    msg,
                    exc if isinstance(exc, FlowApiError) else None,
                    request_type=str(cur.type or "") if cur else "",
                )
                append_request_log(rid, "worker", f"Job failed: {msg}", level="error", data={"api_trace": api_trace})
                fail_result: dict = {
                    "hint": "Mo tab labs.google Flow, Extension OK; thu model Lite hoac Fast",
                    "error": display_msg,
                    "debug_version": 2,
                }
                if isinstance(exc, FlowApiError) and exc.step:
                    fail_result["api_step"] = exc.step
                if api_trace:
                    fail_result["api_attempts"] = api_trace
                    if not fail_result.get("api_step"):
                        fail_result["api_step"] = api_trace[-1].get("label") or "api_error"
                if isinstance(exc, FlowApiError):
                    if not fail_result.get("api_step"):
                        fail_result["api_step"] = exc.step
                    if exc.attempts and not fail_result.get("api_attempts"):
                        fail_result["api_attempts"] = exc.attempts
                    elif exc.raw and not fail_result.get("api_last_response"):
                        from flow2api.services.flow_sdk import compact_api_response

                        fail_result["api_last_response"] = compact_api_response(
                            exc.raw, exc.step or "api_error"
                        )
                activity.update_request(
                    rid,
                    status=f"failed: {display_msg}",
                    error=display_msg,
                    result=fail_result,
                )
                events.publish("request_finished", {"id": rid, "status": "failed"})
            else:
                api_trace = end_api_trace(rid)
                began_trace = False
                append_request_log(
                    rid,
                    "worker",
                    "Job completed",
                    level="info",
                    data={"api_calls": len(api_trace)} if api_trace else None,
                )
        finally:
            if began_trace:
                try:
                    end_api_trace(rid)
                except Exception:
                    pass
            if client is not None:
                client.trace_request_id = None
            unbind_task_profile()
            if profile_id:
                pool.job_finished(profile_id)
            self._cancelled.discard(rid)
            self._running_since.pop(rid, None)
            try:
                cur_after = activity.get_request(rid)
                if cur_after and cur_after.status != "queued":
                    activity.maybe_strip_heavy_params(rid)
            except Exception as exc:
                logger.warning("strip heavy params failed rid=%s: %s", rid[:8], exc)

    async def _process_one(self, rid: str) -> None:
        self._raise_if_cancelled(rid)
        row = activity.get_request(rid)
        if not row:
            return
        params = json.loads(row.params_json or "{}")
        profile_id = str(params.get("profile_id") or "")
        if profile_id:
            from flow2api.services.worker_settings import is_profile_dispatch_enabled

            if not is_profile_dispatch_enabled(profile_id):
                raise RuntimeError("PROFILE_DISPATCH_DISABLED")
        client = get_flow_client()
        if not client.connected and not client.has_direct_lane():
            raise RuntimeError("extension_not_connected")
        if not client.connected and client.has_direct_lane():
            refreshed = await client.ensure_token_fresh()
            if not refreshed or not client.flow_key:
                raise RuntimeError(
                    "offline_auth_expired: Profile offline — không refresh được token từ cookies DB. "
                    "Mở Chrome profile và bấm Get Connection Status."
                )
        elif not client.flow_key:
            refreshed = await client.ensure_token_fresh()
            if not refreshed or not client.flow_key:
                raise RuntimeError("no_flow_token")
        if not client.paygate_tier:
            await client.fetch_paygate_tier()

        profile_id = str(params.get("profile_id") or "")
        req_type = row.type

        if req_type == "upsample_image":
            from flow2api.services.image_upsample import execute_upsample_image_on_client

            append_request_log(
                rid,
                "worker",
                f"Upsample image {str(params.get('media_id') or '')[:12]}…",
                level="info",
            )
            formatted = await execute_upsample_image_on_client(client, params)
            image_urls = []
            if formatted.get("image_url"):
                image_urls = [str(formatted["image_url"])]
            elif formatted.get("url"):
                image_urls = [str(formatted["url"])]
            media_ids = [str(formatted["media_id"])] if formatted.get("media_id") else []
            result = {
                "image_urls": image_urls,
                "media_ids": media_ids,
                "url": formatted.get("url"),
                "image_url": formatted.get("image_url"),
                "data_url": formatted.get("data_url"),
                "project_id": formatted.get("project_id") or params.get("project_id"),
                "profile_id": profile_id,
                "source_media_id": formatted.get("source_media_id"),
                "target_resolution": formatted.get("target_resolution"),
                "upsampled_media_id": formatted.get("upsampled_media_id")
                or formatted.get("media_id"),
            }
            result = await persist_task_result(rid, result, req_type)
            activity.update_request(rid, status="done", result=result, error=None)
            events.publish("request_finished", {"id": rid, "status": "done"})
            return

        if req_type == "upsample_video":
            from flow2api.services.video_upsample import execute_upsample_video_on_client

            project_id = str(params.get("project_id") or "").strip()
            if not project_id:
                raise RuntimeError("missing_project_id")
            append_request_log(
                rid,
                "worker",
                f"Upsample 1080p project {project_id[:12]}…",
                level="info",
            )
            formatted = await execute_upsample_video_on_client(client, params)
            video_urls = list(formatted.get("video_urls") or [])
            if not video_urls and formatted.get("video_url"):
                video_urls = [str(formatted["video_url"])]
            media_ids = [str(formatted["media_id"])] if formatted.get("media_id") else []
            result = {
                "video_urls": video_urls,
                "media_ids": media_ids,
                "project_id": project_id,
                "profile_id": profile_id,
                "source_media_id": formatted.get("source_media_id"),
                "target_resolution": formatted.get("target_resolution"),
                "aspect_ratio": formatted.get("aspect_ratio"),
                "workflow_id": formatted.get("workflow_id"),
                "upsampled_media_id": formatted.get("upsampled_media_id"),
            }
            result = await persist_task_result(rid, result, req_type)
            activity.update_request(rid, status="done", result=result, error=None)
            events.publish("request_finished", {"id": rid, "status": "done"})
            return

        if req_type == "gen_text":
            prompt = _task_prompt(row, params) if row else str(params.get("prompt") or "")
            system_instruction = flow_sdk.coerce_system_instruction(
                params.get("system_instruction")
                or params.get("systemInstruction")
                or ""
            )
            model_name = str(
                params.get("model")
                or params.get("text_model")
                or flow_sdk.DEFAULT_TEXT_MODEL
            )
            raw_level = (
                params.get("thinking_level")
                or params.get("thinkingLevel")
                or flow_sdk.DEFAULT_TEXT_THINKING_LEVEL
            )
            thinking_level = str(raw_level).strip().upper() or flow_sdk.DEFAULT_TEXT_THINKING_LEVEL
            if thinking_level not in flow_sdk.TEXT_THINKING_LEVELS:
                thinking_level = flow_sdk.DEFAULT_TEXT_THINKING_LEVEL
            n_img = len(params.get("image_base64s") or params.get("imageBase64s") or [])
            schema = (
                params.get("schema")
                or params.get("response_schema")
                or params.get("responseSchema")
                or params.get("json_schema")
                or params.get("jsonSchema")
            )
            force_json = bool(
                params.get("json")
                or params.get("force_json")
                or params.get("forceJson")
                or params.get("response_json")
                or schema
            )
            mime = str(
                params.get("response_mime_type")
                or params.get("responseMimeType")
                or (flow_sdk.TEXT_JSON_MIME if force_json else "")
            ).strip()
            append_request_log(
                rid,
                "worker",
                f"gen_text model={model_name} thinking={thinking_level}"
                + (f" images={n_img}" if n_img else "")
                + (" json_schema" if schema else (" json" if force_json else ""))
                + f" prompt={prompt[:80]}…",
                level="info",
            )
            raw = await flow_sdk.gen_text(
                client,
                prompt=prompt,
                system_instruction=system_instruction,
                model=model_name,
                thinking_level=thinking_level,
                contents=params.get("contents") if isinstance(params.get("contents"), list) else None,
                system_parts=params.get("system_parts") or params.get("systemParts"),
                image_base64s=params.get("image_base64s") or params.get("imageBase64s"),
                schema=schema,
                response_mime_type=mime,
                force_json=force_json,
                applet_id=str(params.get("applet_id") or params.get("appletId") or flow_sdk.DEFAULT_TEXT_APPLET_ID),
                applet_version_id=str(
                    params.get("applet_version_id")
                    or params.get("appletVersionId")
                    or flow_sdk.DEFAULT_TEXT_APPLET_VERSION_ID
                ),
                extra_body=params.get("extra_body") if isinstance(params.get("extra_body"), dict) else None,
            )
            result = {
                "text": raw.get("text") or "",
                "json": raw.get("json"),
                "usage": raw.get("usage") or {},
                "model": raw.get("model") or model_name,
                "thinking_level": thinking_level,
                "finish_reason": raw.get("finish_reason") or "",
                "profile_id": profile_id,
            }
            sig = str(raw.get("thought_signature") or "").strip()
            if sig:
                result["thought_signature"] = sig
            activity.update_request(rid, status="done", result=result, error=None)
            events.publish("request_finished", {"id": rid, "status": "done"})
            return

        project_id = await self._ensure_project(profile_id)
        append_request_log(rid, "worker", f"Project ready: {project_id[:12]}…", level="info")

        if req_type == "gen_image":
            raw = await flow_sdk.gen_image(
                client,
                project_id=project_id,
                prompt=_task_prompt(row, params),
                aspect_ratio=params.get("aspect_ratio", "16:9"),
                image_model=params.get("image_model", "NANO_BANANA_PRO"),
                variant_count=int(params.get("variant_count") or 1),
                image_base64s=params.get("image_base64s"),
                image_input_types=params.get("image_input_types"),
            )
            urls = flow_sdk.extract_image_urls(raw)
            media_ids = flow_sdk.extract_image_media_ids(raw)
            media_entries = flow_sdk.build_image_media_entries(raw)
            result = {
                "image_urls": urls,
                "media_ids": media_ids,
                "media_entries": media_entries,
                "project_id": project_id,
                "profile_id": profile_id,
            }
            result = await persist_task_result(rid, result, req_type)
            if (
                params.get("recaptcha_retry_count")
                or params.get("get_media_404_retry_count")
                or params.get("upload_internal_retry_count")
                or params.get("extension_timeout_retry_count")
                or params.get("http_524_retry_count")
                or params.get("http_403_retry_count")
                or params.get("http_429_retry_count")
                or params.get("prominent_people_retry_count")
                or params.get("invalid_argument_retry_count")
                or params.get("trpc_401_retry_count")
                or params.get("retry_not_before")
            ):
                params.pop("recaptcha_retry_count", None)
                params.pop("get_media_404_retry_count", None)
                params.pop("upload_internal_retry_count", None)
                params.pop("extension_timeout_retry_count", None)
                params.pop("http_524_retry_count", None)
                params.pop("http_403_retry_count", None)
                params.pop("http_429_retry_count", None)
                params.pop("prominent_people_retry_count", None)
                params.pop("invalid_argument_retry_count", None)
                params.pop("trpc_401_retry_count", None)
                params.pop("retry_not_before", None)
                self._persist_params(rid, params)
            activity.update_request(rid, status="done", result=result, error=None)
            events.publish("request_finished", {"id": rid, "status": "done"})
            return

        if req_type == "gen_audio":
            dialog = str(
                params.get("dialog")
                or params.get("prompt")
                or (_task_prompt(row, params) if row else "")
                or ""
            ).strip()
            voice = (
                params.get("voice")
                or params.get("reference_audio")
                or params.get("referenceAudio")
                or "achernar"
            )
            audio_model = str(
                params.get("modelKey")
                or params.get("model_key")
                or params.get("audio_model")
                or flow_sdk.DEFAULT_AUDIO_MODEL_KEY
            )
            append_request_log(
                rid,
                "worker",
                f"gen_audio voice={voice} dialog={dialog[:80]}…",
                level="info",
            )
            raw = await flow_sdk.gen_audio(
                client,
                project_id=project_id,
                dialog=dialog,
                voice=str(voice),
                model_key=audio_model,
                generation_type=str(
                    params.get("generation_type")
                    or params.get("generationType")
                    or flow_sdk.AUDIO_GENERATION_TYPE_PREVIEW
                ),
            )
            media_ids = flow_sdk.extract_audio_media_ids(raw)
            urls = await flow_sdk.resolve_audio_urls(client, media_ids)
            media_entries = flow_sdk.build_audio_media_entries(raw, urls)
            result = {
                "audio_urls": urls,
                "media_ids": media_ids,
                "media_entries": media_entries,
                "project_id": project_id,
                "profile_id": profile_id,
                "voice": flow_sdk.normalize_voice_media_id(voice) or "achernar",
                "dialog": dialog,
                "audio_model": audio_model,
                "modelKey": audio_model,
            }
            result = await persist_task_result(rid, result, req_type)
            activity.update_request(rid, status="done", result=result, error=None)
            events.publish("request_finished", {"id": rid, "status": "done"})
            return

        if req_type == "gen_text_video":
            prompt = _task_prompt(row, params) if row else str(params.get("prompt") or "")
            vc = _variant_count(params)
            if flow_sdk.is_omni_flash(get_video_quality(params)):
                duration_s = int(
                    params.get("video_duration_s")
                    or params.get("omni_duration_s")
                    or flow_sdk.OMNI_COMPONENT_DURATION_DEFAULT
                )
                raw = await flow_sdk.gen_omni_text_video(
                    client,
                    project_id=project_id,
                    prompt=prompt,
                    aspect_ratio=params.get("aspect_ratio", "16:9"),
                    duration_s=duration_s,
                    variant_count=vc,
                )
            else:
                raw = await flow_sdk.gen_text_video(
                    client,
                    project_id=project_id,
                    prompt=prompt,
                    aspect_ratio=params.get("aspect_ratio", "16:9"),
                    video_quality=get_video_quality(params, "fast"),
                    variant_count=vc,
                )
            await self._poll_video(rid, raw, project_id)
            return

        if req_type in _IMAGE_VIDEO_TYPES:
            video_mode = _resolve_video_mode(req_type, params)
            if flow_sdk.is_omni_flash(get_video_quality(params)):
                await self._process_omni_video(
                    rid, client, project_id, params, video_mode, req_type
                )
                return
            await self._process_image_video(
                rid, client, project_id, params, video_mode, req_type
            )
            return

        raise RuntimeError(f"unsupported_type:{req_type}")

    async def _process_omni_video(
        self,
        rid: str,
        client: Any,
        project_id: str,
        params: dict[str, Any],
        video_mode: str,
        req_type: str,
    ) -> None:
        row = activity.get_request(rid)
        prompt = _task_prompt(row, params) if row else str(params.get("prompt") or "")
        aspect_ratio = params.get("aspect_ratio", "16:9")
        duration_s = int(
            params.get("video_duration_s")
            or params.get("omni_duration_s")
            or flow_sdk.OMNI_COMPONENT_DURATION_DEFAULT
        )
        vc = _variant_count(params)

        if not self._has_video_input_media(params):
            raw = await flow_sdk.gen_omni_text_video(
                client,
                project_id=project_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                duration_s=duration_s,
                variant_count=vc,
            )
            await self._poll_video(rid, raw, project_id)
            return

        if video_mode == "component":
            ref_ids, video_id = await self._resolve_omni_component_media(
                rid, client, project_id, params
            )
            duration_s = int(
                params.get("video_duration_s")
                or params.get("omni_duration_s")
                or flow_sdk.OMNI_COMPONENT_DURATION_DEFAULT
            )
            if video_id:
                raw = await flow_sdk.gen_omni_edit_video(
                    client,
                    project_id=project_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    reference_media_ids=ref_ids,
                    source_video_media_id=video_id,
                    end_frame_index=flow_sdk.OMNI_COMPONENT_WITH_VIDEO_END_FRAME,
                    variant_count=vc,
                )
            else:
                raw = await flow_sdk.gen_omni_reference_video(
                    client,
                    project_id=project_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    reference_media_ids=ref_ids,
                    duration_s=duration_s,
                    variant_count=vc,
                )
            await self._poll_video(rid, raw, project_id)
            return

        if video_mode != "frame":
            raise RuntimeError(f"invalid_video_mode:{video_mode}")

        if params.get("video_media_id") or params.get("video_base64s"):
            raise RuntimeError("omni_frame_no_video_input")

        duration_s = int(
            params.get("video_duration_s")
            or params.get("omni_duration_s")
            or 4
        )
        image_base64s = params.get("image_base64s") or []
        if params.get("end_media_id") or len(image_base64s) >= 2:
            raise RuntimeError("omni_frame_single_start_image_only")
        start_id = await self._resolve_start_media_id(rid, client, project_id, params)
        raw = await flow_sdk.gen_omni_frame_video(
            client,
            project_id=project_id,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            start_media_id=start_id,
            duration_s=duration_s,
            variant_count=vc,
        )
        await self._poll_video(rid, raw, project_id)

    async def _process_image_video(
        self,
        rid: str,
        client: Any,
        project_id: str,
        params: dict[str, Any],
        video_mode: str,
        req_type: str,
    ) -> None:
        row = activity.get_request(rid)
        prompt = _task_prompt(row, params) if row else str(params.get("prompt") or "")
        aspect_ratio = params.get("aspect_ratio", "16:9")
        video_quality = get_video_quality(params, "fast")
        vc = _variant_count(params)

        if not self._has_video_input_media(params):
            raw = await flow_sdk.gen_text_video(
                client,
                project_id=project_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                video_quality=video_quality,
                variant_count=vc,
            )
            await self._poll_video(rid, raw, project_id)
            return

        if video_mode == "component":
            ref_ids = await self._resolve_reference_media_ids(
                rid, client, project_id, params
            )
            raw = await flow_sdk.gen_multi_image_video(
                client,
                project_id=project_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                video_quality=video_quality,
                reference_media_ids=ref_ids,
                variant_count=vc,
                voice=params.get("voice")
                or params.get("reference_audio")
                or params.get("referenceAudio")
                or params.get("reference_audio_media_id"),
            )
            await self._poll_video(rid, raw, project_id)
            return

        if video_mode != "frame":
            raise RuntimeError(f"invalid_video_mode:{video_mode}")

        image_base64s = params.get("image_base64s") or []
        use_start_end = (
            req_type == "gen_video_start_end"
            or bool(params.get("end_media_id"))
            or len(image_base64s) >= 2
        )
        if use_start_end:
            start_id, end_id = await self._resolve_start_end_media_ids(
                rid, client, project_id, params
            )
            raw = await flow_sdk.gen_video_start_end_image(
                client,
                project_id=project_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                video_quality=video_quality,
                start_media_id=start_id,
                end_media_id=end_id,
                variant_count=vc,
            )
        else:
            start_id = await self._resolve_start_media_id(
                rid, client, project_id, params
            )
            raw = await flow_sdk.gen_video_start_image(
                client,
                project_id=project_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                video_quality=video_quality,
                start_media_id=start_id,
                variant_count=vc,
            )
        await self._poll_video(rid, raw, project_id)

    async def _poll_video(self, rid: str, submit_raw: dict, project_id: str) -> None:
        client = get_flow_client()
        operations = flow_sdk.extract_video_operations(submit_raw)
        if not operations:
            raise RuntimeError(
                "missing_video_operations "
                f"(keys={list(submit_raw.keys()) if isinstance(submit_raw, dict) else type(submit_raw).__name__})"
            )

        media_ids = flow_sdk.collect_video_poll_media_ids(submit_raw, operations)
        submit_media_names = [
            str(m.get("name") or m.get("mediaId"))
            for m in (submit_raw.get("media") or [])
            if isinstance(m, dict) and (m.get("name") or m.get("mediaId"))
        ]
        workflow_primary_ids = [
            str(op.get("_workflow_primary_media_id") or "")
            for op in operations
            if op.get("_workflow_primary_media_id")
        ]
        workflow_ops = [
            {"_primary_media_id": mid, "_workflow_mode": True} for mid in media_ids
        ]
        poll_project_id = flow_sdk.resolve_poll_project_id(submit_raw, operations, project_id)
        source_workflow_id = flow_sdk.extract_workflow_id_from_submit(submit_raw)

        async def _finish(urls: list[str], media: list[str], **extra: Any) -> None:
            row_done = activity.get_request(rid)
            done_params = (
                json.loads(row_done.params_json or "{}") if row_done else {}
            )
            result = {
                "video_urls": urls,
                "media_ids": media,
                "project_id": poll_project_id,
                "profile_id": done_params.get("profile_id"),
                **extra,
            }
            if source_workflow_id:
                result["workflow_id"] = source_workflow_id
            if row_done:
                result = await persist_task_result(rid, result, row_done.type)
            if row_done:
                done_params = json.loads(row_done.params_json or "{}")
                if (
                    done_params.pop("recaptcha_retry_count", None) is not None
                    or done_params.pop("get_media_404_retry_count", None) is not None
                    or done_params.pop("upload_internal_retry_count", None) is not None
                    or done_params.pop("extension_timeout_retry_count", None) is not None
                    or done_params.pop("http_524_retry_count", None) is not None
                    or done_params.pop("http_403_retry_count", None) is not None
                    or done_params.pop("http_429_retry_count", None) is not None
                    or done_params.pop("prominent_people_retry_count", None) is not None
                    or done_params.pop("invalid_argument_retry_count", None) is not None
                    or done_params.pop("trpc_401_retry_count", None) is not None
                    or done_params.pop("retry_not_before", None) is not None
                ):
                    activity.update_request(rid, params=done_params)
            activity.update_request(rid, status="done", result=result, error=None)
            events.publish("request_finished", {"id": rid, "status": "done"})

        workflow_mode = all(op.get("_workflow_mode") for op in operations)

        if media_ids and poll_project_id:
            try:
                urls, media = await flow_sdk.poll_video_by_media(
                    client,
                    poll_project_id,
                    media_ids,
                    VIDEO_POLL_MEDIA_MAX,
                    should_abort=self._abort_hook(rid),
                    requeue_on_get_media_404=workflow_mode,
                )
                await _finish(urls, media, poll_mode="media")
                return
            except RequestCancelled:
                raise
            except GetMedia404Error:
                raise
            except RuntimeError as exc:
                msg = str(exc)
                if msg == "video_generation_failed" or is_prominent_people_filter_failure(exc, msg, None):
                    raise
                if workflow_ops:
                    urls, media = await flow_sdk.poll_workflow_videos(
                        client,
                        workflow_ops,
                        max_rounds=VIDEO_POLL_MEDIA_MAX,
                        project_id=poll_project_id,
                        should_abort=self._abort_hook(rid),
                    )
                    await _finish(urls, media, poll_mode="get_media_fallback", workflow_mode=True)
                    return

        if workflow_mode and workflow_ops:
            urls, media = await flow_sdk.poll_workflow_videos(
                client,
                workflow_ops,
                VIDEO_POLL_MEDIA_MAX,
                project_id=poll_project_id,
                should_abort=self._abort_hook(rid),
            )
            await _finish(urls, media, poll_mode="get_media", workflow_mode=True)
            return

        current_ops = operations
        transient_streak = 0
        for round_idx in range(VIDEO_POLL_MAX):
            self._raise_if_cancelled(rid)
            await flow_sdk.video_poll_wait(round_idx)
            poll = await flow_sdk.check_async_operations(client, current_ops)
            transient = poll.get("_transient_error")
            if transient and not poll.get("operations"):
                if flow_sdk.is_transient_flow_error(str(transient)):
                    transient_streak += 1
                    logger.warning("video poll transient (%s): %s", transient_streak, transient)
                    await asyncio.sleep(
                        min(30, VIDEO_POLL_INTERVAL_S * min(transient_streak, 6))
                    )
                    if media_ids and poll_project_id and transient_streak % 3 == 0:
                        urls, media = await flow_sdk.poll_video_by_media(
                            client,
                            poll_project_id,
                            media_ids,
                            max_rounds=5,
                            should_abort=self._abort_hook(rid),
                            skip_first_delay=True,
                        )
                        if urls:
                            await _finish(urls, media, recovered_from="media_poll")
                            return
                    elif workflow_ops and transient_streak % 3 == 0:
                        urls, media = await flow_sdk.poll_workflow_videos(
                            client,
                            workflow_ops,
                            max_rounds=5,
                            project_id=poll_project_id,
                            should_abort=self._abort_hook(rid),
                            skip_first_delay=True,
                        )
                        if urls:
                            await _finish(urls, media, recovered_from="get_media")
                            return
                    continue
                raise RuntimeError(str(transient))

            transient_streak = 0
            current_ops = poll.get("operations") or current_ops
            summary = flow_sdk.summarize_video_poll(poll)
            if any(s.get("error") or "FAILED" in str(s.get("status", "")).upper() for s in summary):
                if media_ids and poll_project_id:
                    try:
                        urls, media = await flow_sdk.poll_video_by_media(
                            client,
                            poll_project_id,
                            media_ids,
                            max_rounds=15,
                            should_abort=self._abort_hook(rid),
                            skip_first_delay=True,
                        )
                        if urls:
                            await _finish(urls, media, recovered_from="media_poll")
                            return
                    except RequestCancelled:
                        raise
                    except GetMedia404Error:
                        raise
                    except RuntimeError:
                        pass
                if workflow_ops:
                    try:
                        urls, media = await flow_sdk.poll_workflow_videos(
                            client,
                            workflow_ops,
                            max_rounds=10,
                            project_id=poll_project_id,
                            should_abort=self._abort_hook(rid),
                            skip_first_delay=True,
                        )
                        if urls:
                            await _finish(urls, media, recovered_from="get_media")
                            return
                    except GetMedia404Error:
                        raise
                raise RuntimeError("video_generation_failed")
            if summary and all(s.get("done") for s in summary):
                urls, media = flow_sdk._urls_from_operations(current_ops)
                await _finish(urls, media, operations=summary)
                return
            if media_ids and poll_project_id and round_idx % 5 == 4:
                try:
                    urls, media = await flow_sdk.poll_video_by_media(
                        client,
                        poll_project_id,
                        media_ids,
                        max_rounds=3,
                        should_abort=self._abort_hook(rid),
                        skip_first_delay=True,
                    )
                    if urls:
                        await _finish(urls, media, recovered_from="media_poll")
                        return
                except RequestCancelled:
                    raise
                except GetMedia404Error:
                    raise
                except RuntimeError:
                    pass
            elif workflow_ops and round_idx % 5 == 4:
                try:
                    urls, media = await flow_sdk.poll_workflow_videos(
                        client,
                        workflow_ops,
                        max_rounds=3,
                        project_id=poll_project_id,
                        should_abort=self._abort_hook(rid),
                        skip_first_delay=True,
                    )
                    if urls:
                        await _finish(urls, media, recovered_from="get_media")
                        return
                except GetMedia404Error:
                    raise

        if media_ids and poll_project_id:
            try:
                urls, media = await flow_sdk.poll_video_by_media(
                    client,
                    poll_project_id,
                    media_ids,
                    max_rounds=30,
                    should_abort=self._abort_hook(rid),
                    skip_first_delay=True,
                )
                if urls:
                    await _finish(urls, media, recovered_from="media_poll")
                    return
            except RequestCancelled:
                raise
            except GetMedia404Error:
                raise
            except RuntimeError:
                pass
        if workflow_ops:
            try:
                urls, media = await flow_sdk.poll_workflow_videos(
                    client,
                    workflow_ops,
                    max_rounds=30,
                    project_id=poll_project_id,
                    should_abort=self._abort_hook(rid),
                    skip_first_delay=True,
                )
                if urls:
                    await _finish(urls, media, recovered_from="get_media")
                    return
            except GetMedia404Error:
                raise
        raise RuntimeError("timeout_waiting_video")


_worker: WorkerController | None = None


def get_worker() -> WorkerController:
    global _worker
    if _worker is None:
        _worker = WorkerController()
    return _worker
