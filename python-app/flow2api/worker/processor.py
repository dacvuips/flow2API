from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from flow2api.config import IMAGE_POLL_MAX, POLL_INTERVAL_S, RECAPTCHA_RETRY_MAX, VIDEO_POLL_MAX
from flow2api.services.worker_settings import get_worker_settings
from flow2api.services import activity, flow_sdk
from flow2api.services.api_trace import begin_api_trace, end_api_trace
from flow2api.services.flow_sdk import FlowApiError, format_api_error, is_get_media_404_error
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.request_logs import append_request_log
from flow2api.services.flow_client import (
    bind_task_profile,
    get_flow_client,
    pick_profile_for_task,
    unbind_task_profile,
)
logger = logging.getLogger(__name__)

_IMAGE_VIDEO_TYPES = frozenset(
    {"gen_image_video", "gen_video", "gen_video_start_end", "gen_multi_image_video"}
)


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


class WorkerController:
    def __init__(self) -> None:
        self._scheduler_task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
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
            self._cancelled.discard(rid)

    def request_cancel(self, rid: str) -> None:
        self._cancelled.add(rid)

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
        profile_id = pick_profile_for_task(str(existing) if existing else None)
        if not profile_id:
            raise RuntimeError("no_extension_profile_online")
        session = get_extension_pool().get(profile_id)
        params["profile_id"] = profile_id
        if session:
            params["profile_label"] = session.display_name()
            if session.email:
                params["profile_email"] = session.email
        self._persist_params(rid, params)
        return profile_id

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            self._prune_running()
            settings = get_worker_settings()
            slots = settings.max_concurrent - len(self._running)
            started = 0
            pool = get_extension_pool()
            if slots > 0 and pool.ready_count() == 0:
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            if slots > 0 and not pool.has_available_profile():
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            if slots > 0:
                rows = activity.next_queued_batch(max(slots * 2, slots))
                started_this_round = 0
                for row in rows:
                    if started_this_round >= slots:
                        break
                    if row.id in self._running:
                        continue
                    row_params = json.loads(row.params_json or "{}")
                    if not pick_profile_for_task(row_params.get("profile_id")):
                        continue
                    stagger = settings.task_stagger_s
                    if stagger > 0 and self._last_start_monotonic > 0:
                        wait_s = stagger - (time.monotonic() - self._last_start_monotonic)
                        if wait_s > 0:
                            await asyncio.sleep(wait_s)
                    activity.update_request(row.id, status="running")
                    events.publish("request_started", {"id": row.id})
                    self._running[row.id] = asyncio.create_task(self._run_job(row.id))
                    self._last_start_monotonic = time.monotonic()
                    started += 1
                    started_this_round += 1
            if started == 0 and not self._running:
                await asyncio.sleep(POLL_INTERVAL_S)
            else:
                await asyncio.sleep(0.25)

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
            append_request_log(rid, "worker", "Job canceled", level="warn")
            cur = activity.get_request(rid)
            if cur and cur.status in ("queued", "running"):
                activity.update_request(
                    rid,
                    status="failed: canceled",
                    error="canceled",
                    result={"error": "canceled"},
                )
            events.publish("request_finished", {"id": rid, "status": "canceled"})
        except Exception as exc:
            api_trace = end_api_trace(rid)
            msg = format_api_error(exc)
            cur = activity.get_request(rid)
            retry_params = json.loads(cur.params_json or "{}") if cur else {}
            recaptcha_retry = int(retry_params.get("recaptcha_retry_count") or 0)
            if flow_sdk.is_recaptcha_error(msg) and recaptcha_retry < RECAPTCHA_RETRY_MAX:
                retry_params["recaptcha_retry_count"] = recaptcha_retry + 1
                activity.update_request(
                    rid,
                    status="queued",
                    params=retry_params,
                    error=msg,
                )
                logger.warning(
                    "reCAPTCHA retry %s/%s rid=%s — giữ media upload, thử lại",
                    recaptcha_retry + 1,
                    RECAPTCHA_RETRY_MAX,
                    rid[:8],
                )
                events.publish("request_finished", {"id": rid, "status": "queued"})
                return
            get_media_404_retry = int(retry_params.get("get_media_404_retry_count") or 0)
            if is_get_media_404_error(exc) and get_media_404_retry < RECAPTCHA_RETRY_MAX:
                retry_params["get_media_404_retry_count"] = get_media_404_retry + 1
                activity.update_request(
                    rid,
                    status="queued",
                    params=retry_params,
                    error=msg,
                )
                logger.warning(
                    "get_media 404 retry %s/%s rid=%s — đưa lại queue, thử lại",
                    get_media_404_retry + 1,
                    RECAPTCHA_RETRY_MAX,
                    rid[:8],
                )
                events.publish("request_finished", {"id": rid, "status": "queued"})
                return
            logger.exception("worker failed rid=%s api_trace=%s", rid, len(api_trace))
            append_request_log(rid, "worker", f"Job failed: {msg}", level="error", data={"api_trace": api_trace})
            fail_result: dict = {
                "hint": "Mo tab labs.google Flow, Extension OK; thu model Lite hoac Fast",
                "error": msg,
                "debug_version": 2,
            }
            if api_trace:
                fail_result["api_attempts"] = api_trace
                fail_result["api_step"] = api_trace[-1].get("label") or "api_error"
            elif isinstance(exc, FlowApiError):
                fail_result["api_step"] = exc.step
                if exc.attempts:
                    fail_result["api_attempts"] = exc.attempts
                elif exc.raw:
                    from flow2api.services.flow_sdk import compact_api_response

                    fail_result["api_last_response"] = compact_api_response(
                        exc.raw, exc.step or "api_error"
                    )
            activity.update_request(
                rid,
                status=f"failed: {msg}",
                error=msg,
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
                prompt=params.get("prompt", ""),
                aspect_ratio=params.get("aspect_ratio", "16:9"),
                image_model=params.get("image_model", "NANO_BANANA_PRO"),
                variant_count=int(params.get("variant_count") or 1),
                image_base64s=params.get("image_base64s"),
                image_input_types=params.get("image_input_types"),
            )
            urls = flow_sdk.extract_image_urls(raw)
            media_ids = flow_sdk.extract_image_media_ids(raw)
            result = {"image_urls": urls, "media_ids": media_ids}
            if params.get("recaptcha_retry_count") or params.get("get_media_404_retry_count"):
                params.pop("recaptcha_retry_count", None)
                params.pop("get_media_404_retry_count", None)
                self._persist_params(rid, params)
            activity.update_request(rid, status="done", result=result)
            events.publish("request_finished", {"id": rid, "status": "done"})
            return

        if req_type == "gen_text_video":
            raw = await flow_sdk.gen_text_video(
                client,
                project_id=project_id,
                prompt=params.get("prompt", ""),
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
        prompt = params.get("prompt", "")
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
                done_params = json.loads(row_done.params_json or "{}")
                if done_params.pop("recaptcha_retry_count", None) is not None or done_params.pop(
                    "get_media_404_retry_count", None
                ) is not None:
                    activity.update_request(rid, params=done_params)
            activity.update_request(rid, status="done", result=result)
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
                )
                await _finish(urls, media, poll_mode="media")
                return
            except RequestCancelled:
                raise
            except RuntimeError as exc:
                msg = str(exc)
                if msg == "video_generation_failed":
                    raise
                if workflow_ops:
                    urls, media = await flow_sdk.poll_workflow_videos(
                        client,
                        workflow_ops,
                        max_rounds=min(60, VIDEO_POLL_MAX),
                        should_abort=self._abort_hook(rid),
                    )
                    await _finish(urls, media, poll_mode="get_media_fallback", workflow_mode=True)
                    return

        if workflow_mode and workflow_ops:
            urls, media = await flow_sdk.poll_workflow_videos(
                client,
                workflow_ops,
                VIDEO_POLL_MAX,
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
                            client, workflow_ops, max_rounds=5, should_abort=self._abort_hook(rid)
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
                    except RuntimeError:
                        pass
                if workflow_ops:
                    urls, media = await flow_sdk.poll_workflow_videos(
                        client, workflow_ops, max_rounds=10, should_abort=self._abort_hook(rid)
                    )
                    if urls:
                        await _finish(urls, media, recovered_from="get_media")
                        return
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
                except RuntimeError:
                    pass
            elif workflow_ops and round_idx % 5 == 4:
                urls, media = await flow_sdk.poll_workflow_videos(
                    client, workflow_ops, max_rounds=3, should_abort=self._abort_hook(rid)
                )
                if urls:
                    await _finish(urls, media, recovered_from="get_media")
                    return
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
            except RuntimeError:
                pass
        if workflow_ops:
            urls, media = await flow_sdk.poll_workflow_videos(
                client, workflow_ops, max_rounds=30, should_abort=self._abort_hook(rid)
            )
            if urls:
                await _finish(urls, media, recovered_from="get_media")
                return
        raise RuntimeError("timeout_waiting_video")


_worker: WorkerController | None = None


def get_worker() -> WorkerController:
    global _worker
    if _worker is None:
        _worker = WorkerController()
    return _worker
