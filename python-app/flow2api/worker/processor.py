from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from flow2api.config import (
    IMAGE_POLL_MAX,
    POLL_INTERVAL_S,
    RECAPTCHA_RETRY_MAX,
    TASK_RUNNING_TIMEOUT_MAX_RETRIES,
    TASK_RUNNING_TIMEOUT_S,
    VIDEO_POLL_MAX,
    WORKER_NUDGE_STUCK_S,
)
from flow2api.services.worker_settings import get_worker_settings
from flow2api.services import activity, flow_sdk
from flow2api.services.api_trace import begin_api_trace, end_api_trace
from flow2api.services.flow_sdk import (
    FlowApiError,
    GetMedia404Error,
    format_api_error,
    is_extension_timeout_error,
    is_get_media_404_failure,
    is_invalid_argument_retry_failure,
    is_prominent_people_filter_failure,
    is_trpc_401_failure,
    is_upload_image_internal_failure,
)
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.request_logs import append_request_log
from flow2api.services.result_media import prepare_params_for_worker_requeue
from flow2api.services.stored_media import persist_task_result
from flow2api.services.flow_client import (
    apply_retry_profile_rotation,
    bind_task_profile,
    get_flow_client,
    pick_profile_for_retry,
    pick_profile_for_task,
    profile_available_for_queue,
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


class RequestCancelled(RuntimeError):
    """Raised when user stops a queued/running request."""


TASK_TIMEOUT_ERROR = "task_timeout_5m"


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
            self._running.pop(rid, None)
            self._running_since.pop(rid, None)
            self._cancelled.discard(rid)

    def request_cancel(self, rid: str) -> None:
        self._cancelled.add(rid)

    def cancel_running_tasks(self, ids: set[str] | None = None) -> None:
        """Cancel in-flight asyncio tasks (optional subset by request id)."""
        self._prune_running()
        targets = set(ids) if ids is not None else set(self._running.keys())
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
        """Requeue running task when stuck; fail after max retries."""
        row = activity.get_request(rid)
        if not row or row.status != "running":
            return

        params = json.loads(row.params_json or "{}")
        retry_count = int(params.get("running_timeout_retry_count") or 0)
        max_retries = max(1, int(TASK_RUNNING_TIMEOUT_MAX_RETRIES or 3))
        task = self._running.get(rid)
        self._running_since.pop(rid, None)

        if retry_count < max_retries:
            params["running_timeout_retry_count"] = retry_count + 1
            params = prepare_params_for_worker_requeue(
                params,
                rid,
                prompt=str(row.prompt or ""),
            )
            params = apply_retry_profile_rotation(params)
            activity.update_request(
                rid,
                status="queued",
                params=params,
                error=None,
            )
            append_request_log(
                rid,
                "worker",
                (
                    f"Running stuck {TASK_RUNNING_TIMEOUT_S // 60}m — "
                    f"requeue {retry_count + 1}/{max_retries} "
                    f"(same task id, inputs restored)"
                ),
                level="warn",
            )
            events.publish("request_finished", {"id": rid, "status": "queued"})
            logger.warning(
                "task stuck running rid=%s — requeue %s/%s, inputs restored",
                rid[:8],
                retry_count + 1,
                max_retries,
            )
            if task and not task.done():
                task.cancel()
            self._running.pop(rid, None)
            self._cancelled.discard(rid)
            return

        activity.update_request(
            rid,
            status=f"failed: {TASK_TIMEOUT_ERROR}",
            error=TASK_TIMEOUT_ERROR,
            result={"error": TASK_TIMEOUT_ERROR},
        )
        append_request_log(
            rid,
            "worker",
            (
                f"Job timed out after {max_retries} retries "
                f"({TASK_RUNNING_TIMEOUT_S // 60}m each)"
            ),
            level="warn",
        )
        events.publish("request_finished", {"id": rid, "status": "failed"})
        logger.warning(
            "task timed out rid=%s after %s retries x %ss",
            rid[:8],
            max_retries,
            TASK_RUNNING_TIMEOUT_S,
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
        active_limit = max(60, int(TASK_RUNNING_TIMEOUT_S or 300))
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

    def _abort_hook(self, rid: str):
        return lambda: self._raise_if_cancelled(rid)

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
        return any(bool(x) for x in (params.get("image_base64s") or []))

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

    def _assign_profile(self, rid: str, params: dict[str, Any]) -> str:
        existing = params.get("profile_id")
        exclude = params.get("retry_exclude_profile_id")
        if existing:
            profile_id = pick_profile_for_task(str(existing))
        elif exclude:
            profile_id = pick_profile_for_retry(str(exclude))
        else:
            profile_id = pick_profile_for_task(None)
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
        return profile_id

    def _requeue_for_retry(
        self,
        rid: str,
        params: dict[str, Any],
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        params = apply_retry_profile_rotation(params)
        params.pop("running_started_at", None)
        activity.update_request(rid, status="queued", params=params, error=None)
        events.publish("request_finished", {"id": rid, "status": "queued"})
        return params

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
        slots = settings.max_concurrent - len(self._running)
        started = 0
        pool = get_extension_pool()
        if slots > 0 and pool.ready_count() == 0:
            return 0
        if slots > 0 and not pool.has_available_profile():
            return 0
        if slots > 0:
            rows = activity.next_queued_batch(max(slots * 2, slots))
            started_this_round = 0
            for row in rows:
                if started_this_round >= slots:
                    break
                if row.id in self._running:
                    continue
                row_params = json.loads(row.params_json or "{}")
                retry_not_before = float(row_params.get("retry_not_before") or 0)
                if retry_not_before > time.time():
                    continue
                if not profile_available_for_queue(row_params):
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
        row = activity.get_request(rid)
        if not row:
            return
        params = json.loads(row.params_json or "{}")
        profile_id = self._assign_profile(rid, params)
        pool = get_extension_pool()
        pool.job_started(profile_id)
        bind_task_profile(profile_id)
        client = get_flow_client()
        client.trace_request_id = rid
        begin_api_trace(rid)
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
            cur = activity.get_request(rid)
            if cur and (
                cur.status == "queued" or cur.status.startswith("failed:")
            ):
                raise
            if cur and cur.status == "running":
                activity.update_request(
                    rid,
                    status=f"failed: {TASK_TIMEOUT_ERROR}",
                    error=TASK_TIMEOUT_ERROR,
                    result={"error": TASK_TIMEOUT_ERROR},
                )
                append_request_log(
                    rid,
                    "worker",
                    f"Job canceled (timeout {TASK_RUNNING_TIMEOUT_S // 60}m)",
                    level="warn",
                )
                events.publish("request_finished", {"id": rid, "status": "failed"})
            raise
        except Exception as exc:
            cur = activity.get_request(rid)
            if cur and (
                cur.status.startswith("failed:")
                or cur.error in (TASK_TIMEOUT_ERROR, "canceled")
            ):
                return
            api_trace = end_api_trace(rid)
            msg = format_api_error(exc).strip() or "unknown_error"
            cur = activity.get_request(rid)
            retry_params = json.loads(cur.params_json or "{}") if cur else {}
            recaptcha_retry = int(retry_params.get("recaptcha_retry_count") or 0)
            if flow_sdk.is_recaptcha_error(msg) and recaptcha_retry < RECAPTCHA_RETRY_MAX:
                delay_s = flow_sdk.recaptcha_retry_delay(recaptcha_retry)
                retry_params["recaptcha_retry_count"] = recaptcha_retry + 1
                retry_params["retry_not_before"] = time.time() + delay_s
                retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                logger.warning(
                    "reCAPTCHA retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                    recaptcha_retry + 1,
                    RECAPTCHA_RETRY_MAX,
                    rid[:8],
                    delay_s,
                    str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                )
                return
            get_media_404_retry = int(retry_params.get("get_media_404_retry_count") or 0)
            if is_get_media_404_failure(exc, msg, api_trace) and get_media_404_retry < RECAPTCHA_RETRY_MAX:
                retry_params["get_media_404_retry_count"] = get_media_404_retry + 1
                row_prompt = str((cur.prompt if cur else "") or "")
                retry_params = prepare_params_for_worker_requeue(
                    retry_params,
                    rid,
                    prompt=row_prompt,
                )
                retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                append_request_log(
                    rid,
                    "worker",
                    (
                        f"get_media 404 during poll — requeue "
                        f"{get_media_404_retry + 1}/{RECAPTCHA_RETRY_MAX} "
                        f"(same task id, inputs restored)"
                    ),
                    level="warn",
                )
                logger.warning(
                    "get_media 404 retry %s/%s rid=%s — inputs restored, profile=%s",
                    get_media_404_retry + 1,
                    RECAPTCHA_RETRY_MAX,
                    rid[:8],
                    str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                )
                return
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
            prominent_people_retry = int(retry_params.get("prominent_people_retry_count") or 0)
            if (
                is_prominent_people_filter_failure(exc, msg, api_trace)
                and prominent_people_retry < RECAPTCHA_RETRY_MAX
            ):
                delay_s = flow_sdk.recaptcha_retry_delay(prominent_people_retry)
                retry_params["prominent_people_retry_count"] = prominent_people_retry + 1
                retry_params["retry_not_before"] = time.time() + delay_s
                retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                logger.warning(
                    "prominent_people filter retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                    prominent_people_retry + 1,
                    RECAPTCHA_RETRY_MAX,
                    rid[:8],
                    delay_s,
                    str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
                )
                return
            invalid_argument_retry = int(retry_params.get("invalid_argument_retry_count") or 0)
            if (
                is_invalid_argument_retry_failure(exc, msg, api_trace)
                and invalid_argument_retry < RECAPTCHA_RETRY_MAX
            ):
                delay_s = flow_sdk.recaptcha_retry_delay(invalid_argument_retry)
                retry_params["invalid_argument_retry_count"] = invalid_argument_retry + 1
                retry_params["retry_not_before"] = time.time() + delay_s
                retry_params = self._requeue_for_retry(rid, retry_params, error=msg)
                logger.warning(
                    "INVALID_ARGUMENT / PUBLIC_ERROR_MINOR retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                    invalid_argument_retry + 1,
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
                logger.warning(
                    "TRPC_401 retry %s/%s rid=%s — chờ %.1fs, profile=%s",
                    trpc_401_retry + 1,
                    RECAPTCHA_RETRY_MAX,
                    rid[:8],
                    delay_s,
                    str(retry_params.get("profile_id") or retry_params.get("retry_exclude_profile_id") or "-")[:12],
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
            display_msg = flow_sdk.sanitize_public_error(msg)
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
            append_request_log(
                rid,
                "worker",
                "Job completed",
                level="info",
                data={"api_calls": len(api_trace)} if api_trace else None,
            )
        finally:
            client.trace_request_id = None
            unbind_task_profile()
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
        client = get_flow_client()
        if not client.connected:
            raise RuntimeError("extension_not_connected")
        if not client.flow_key:
            raise RuntimeError("no_flow_token")
        if not client.paygate_tier:
            await client.fetch_paygate_tier()

        profile_id = str(params.get("profile_id") or "")
        project_id = await self._ensure_project(profile_id)
        append_request_log(rid, "worker", f"Project ready: {project_id[:12]}…", level="info")
        req_type = row.type

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
            }
            result = await persist_task_result(rid, result, req_type)
            if (
                params.get("recaptcha_retry_count")
                or params.get("get_media_404_retry_count")
                or params.get("upload_internal_retry_count")
                or params.get("extension_timeout_retry_count")
                or params.get("prominent_people_retry_count")
                or params.get("invalid_argument_retry_count")
                or params.get("trpc_401_retry_count")
                or params.get("retry_not_before")
            ):
                params.pop("recaptcha_retry_count", None)
                params.pop("get_media_404_retry_count", None)
                params.pop("upload_internal_retry_count", None)
                params.pop("extension_timeout_retry_count", None)
                params.pop("prominent_people_retry_count", None)
                params.pop("invalid_argument_retry_count", None)
                params.pop("trpc_401_retry_count", None)
                params.pop("retry_not_before", None)
                self._persist_params(rid, params)
            activity.update_request(rid, status="done", result=result, error=None)
            events.publish("request_finished", {"id": rid, "status": "done"})
            return

        if req_type == "gen_text_video":
            raw = await flow_sdk.gen_text_video(
                client,
                project_id=project_id,
                prompt=_task_prompt(row, params),
                aspect_ratio=params.get("aspect_ratio", "16:9"),
                video_quality=params.get("video_quality", "fast"),
            )
            await self._poll_video(rid, raw, project_id)
            return

        if req_type in _IMAGE_VIDEO_TYPES:
            video_mode = _resolve_video_mode(req_type, params)
            await self._process_image_video(
                rid, client, project_id, params, video_mode, req_type
            )
            return

        raise RuntimeError(f"unsupported_type:{req_type}")

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
        video_quality = params.get("video_quality", "fast")

        if not self._has_video_input_media(params):
            raw = await flow_sdk.gen_text_video(
                client,
                project_id=project_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                video_quality=video_quality,
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

        async def _finish(urls: list[str], media: list[str], **extra: Any) -> None:
            result = {"video_urls": urls, "media_ids": media, **extra}
            row_done = activity.get_request(rid)
            if row_done:
                result = await persist_task_result(rid, result, row_done.type)
            if row_done:
                done_params = json.loads(row_done.params_json or "{}")
                if (
                    done_params.pop("recaptcha_retry_count", None) is not None
                    or done_params.pop("get_media_404_retry_count", None) is not None
                    or done_params.pop("upload_internal_retry_count", None) is not None
                    or done_params.pop("extension_timeout_retry_count", None) is not None
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
                    VIDEO_POLL_MAX,
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
                        max_rounds=min(60, VIDEO_POLL_MAX),
                        project_id=poll_project_id,
                        should_abort=self._abort_hook(rid),
                    )
                    await _finish(urls, media, poll_mode="get_media_fallback", workflow_mode=True)
                    return

        if workflow_mode and workflow_ops:
            urls, media = await flow_sdk.poll_workflow_videos(
                client,
                workflow_ops,
                VIDEO_POLL_MAX,
                project_id=poll_project_id,
                should_abort=self._abort_hook(rid),
            )
            await _finish(urls, media, poll_mode="get_media", workflow_mode=True)
            return

        current_ops = operations
        transient_streak = 0
        for round_idx in range(VIDEO_POLL_MAX):
            self._raise_if_cancelled(rid)
            poll = await flow_sdk.check_async_operations(client, current_ops)
            transient = poll.get("_transient_error")
            if transient and not poll.get("operations"):
                if flow_sdk.is_transient_flow_error(str(transient)):
                    transient_streak += 1
                    logger.warning("video poll transient (%s): %s", transient_streak, transient)
                    await asyncio.sleep(min(30, POLL_INTERVAL_S * min(transient_streak, 6)))
                    if media_ids and poll_project_id and transient_streak % 3 == 0:
                        urls, media = await flow_sdk.poll_video_by_media(
                            client,
                            poll_project_id,
                            media_ids,
                            max_rounds=5,
                            should_abort=self._abort_hook(rid),
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
                    )
                    if urls:
                        await _finish(urls, media, recovered_from="get_media")
                        return
                except GetMedia404Error:
                    raise
            await asyncio.sleep(POLL_INTERVAL_S)

        if media_ids and poll_project_id:
            try:
                urls, media = await flow_sdk.poll_video_by_media(
                    client,
                    poll_project_id,
                    media_ids,
                    max_rounds=30,
                    should_abort=self._abort_hook(rid),
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
