"""ChatGPT HTTP broker — dashboard → agent queue → extension poll (không cần Bridge WS).

Cũng giữ hàng đợi job public async (POST trả id ngay → client poll GET)
để tránh Cloudflare 524 khi chat chạy lâu.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

POLL_TIMEOUT_S = 25.0
DEFAULT_JOB_TIMEOUT_S = 180.0
WORKER_STALE_S = 60.0
PUBLIC_JOB_TTL_S = 30 * 60  # giữ result ~30 phút
PUBLIC_JOB_MAX = 200


@dataclass
class ChatgptJob:
    job_id: str
    params: dict[str, Any]
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    claimed_by: str = ""
    claimed_at: float = 0.0


@dataclass
class WorkerState:
    worker_id: str
    label: str = ""
    last_seen_at: float = field(default_factory=time.time)

    def is_online(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_seen_at) < WORKER_STALE_S


@dataclass
class PublicChatJob:
    """Job cho API public async — tách khỏi job extension poll."""

    job_id: str
    status: str = "queued"  # queued | running | done | failed | cancelled
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    result: dict[str, Any] | None = None
    params_summary: dict[str, Any] = field(default_factory=dict)
    kwargs: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "params": self.params_summary,
            "profile_id": self.params_summary.get("assigned_profile_id")
            or self.params_summary.get("profile_id"),
        }
        if include_result and self.result is not None:
            out["result"] = self.result
        return out


class ChatgptBroker:
    def __init__(self) -> None:
        self._jobs: dict[str, ChatgptJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: dict[str, WorkerState] = {}
        self._waiters: list[asyncio.Future] = []
        self._public_jobs: dict[str, PublicChatJob] = {}
        self._public_tasks: dict[str, asyncio.Task] = {}
        self._public_kwargs: dict[str, dict[str, Any]] = {}

    def touch_worker(self, worker_id: str, label: str = "") -> None:
        wid = (worker_id or "").strip()
        if not wid:
            return
        st = self._workers.get(wid)
        if not st:
            st = WorkerState(worker_id=wid, label=label or "")
            self._workers[wid] = st
        else:
            st.last_seen_at = time.time()
            if label:
                st.label = label

    def online_workers(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for st in self._workers.values():
            if st.is_online(now):
                out.append(
                    {
                        "worker_id": st.worker_id,
                        "label": st.label,
                        "last_seen_at": st.last_seen_at,
                    }
                )
        return out

    def _wake_waiters(self) -> None:
        waiters = self._waiters
        self._waiters = []
        for fut in waiters:
            if not fut.done():
                fut.set_result(True)

    async def submit(self, params: dict[str, Any], timeout: float = DEFAULT_JOB_TIMEOUT_S) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        job_id = str(uuid.uuid4())
        fut: asyncio.Future = loop.create_future()
        job = ChatgptJob(job_id=job_id, params=dict(params or {}), future=fut)
        self._jobs[job_id] = job
        await self._queue.put(job_id)
        self._wake_waiters()
        logger.info("chatgpt job queued %s (queue=%s workers=%s)", job_id[:8], self._queue.qsize(), len(self.online_workers()))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            self._jobs.pop(job_id, None)
            if not fut.done():
                fut.cancel()
            return {"ok": False, "error": "chatgpt_job_timeout"}
        finally:
            self._jobs.pop(job_id, None)

    async def poll(
        self,
        worker_id: str,
        *,
        label: str = "",
        timeout: float = POLL_TIMEOUT_S,
    ) -> list[dict[str, Any]]:
        self.touch_worker(worker_id, label=label)
        deadline = time.time() + max(1.0, min(float(timeout), 60.0))

        while True:
            try:
                job_id = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                job_id = None

            if job_id:
                job = self._jobs.get(job_id)
                if not job or job.future.done():
                    continue
                job.claimed_by = worker_id
                job.claimed_at = time.time()
                return [
                    {
                        "jobId": job.job_id,
                        "params": job.params,
                    }
                ]

            remaining = deadline - time.time()
            if remaining <= 0:
                return []

            loop = asyncio.get_running_loop()
            waiter: asyncio.Future = loop.create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, timeout=remaining)
            except asyncio.TimeoutError:
                return []
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def complete(self, job_id: str, result: dict[str, Any] | None = None, error: str | None = None) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.future.done():
            return False
        payload = dict(result or {})
        if error and not payload.get("error"):
            payload = {**payload, "ok": False, "error": error}
        if "ok" not in payload:
            payload["ok"] = not bool(payload.get("error"))
        job.future.set_result(payload)
        self._jobs.pop(job_id, None)
        return True

    # ── Public async jobs (Cloudflare-safe) ─────────────────────────────

    def _purge_public_jobs(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, job in self._public_jobs.items()
            if (now - job.updated_at) > PUBLIC_JOB_TTL_S
        ]
        for jid in stale:
            self._public_jobs.pop(jid, None)
            self._public_kwargs.pop(jid, None)
            task = self._public_tasks.pop(jid, None)
            if task and not task.done():
                task.cancel()
        # Cap memory: drop oldest finished jobs first
        if len(self._public_jobs) > PUBLIC_JOB_MAX:
            finished = sorted(
                (
                    (jid, job)
                    for jid, job in self._public_jobs.items()
                    if job.status in ("done", "failed", "cancelled")
                ),
                key=lambda x: x[1].updated_at,
            )
            overflow = len(self._public_jobs) - PUBLIC_JOB_MAX
            for jid, _ in finished[: max(0, overflow)]:
                self._public_jobs.pop(jid, None)
                self._public_kwargs.pop(jid, None)
                self._public_tasks.pop(jid, None)

    def _public_params_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        images = params.get("images") or []
        profile_id = params.get("profile_id")
        return {
            "prompt_preview": str(params.get("prompt") or "")[:120],
            "model": params.get("model"),
            "endpoint": params.get("endpoint"),
            "profile_id": profile_id,
            "assigned_profile_id": params.get("assigned_profile_id") or profile_id,
            "profile_assigned_by_user": bool(params.get("profile_assigned_by_user")),
            "conversation_id": params.get("conversation_id"),
            "parent_message_id": params.get("parent_message_id"),
            "image_count": len(images) if isinstance(images, list) else 0,
            "mode": params.get("mode"),
        }

    def create_public_job(
        self,
        params: dict[str, Any],
        *,
        kwargs: dict[str, Any] | None = None,
    ) -> PublicChatJob:
        self._purge_public_jobs()
        job_id = str(uuid.uuid4())
        summary = self._public_params_summary(params)
        job = PublicChatJob(
            job_id=job_id,
            status="queued",
            params_summary=summary,
            kwargs=dict(kwargs or {}),
        )
        self._public_jobs[job_id] = job
        self._public_kwargs[job_id] = dict(kwargs or {})
        return job

    def get_public_job(self, job_id: str) -> PublicChatJob | None:
        self._purge_public_jobs()
        return self._public_jobs.get(job_id)

    def iter_public_jobs(self) -> list[PublicChatJob]:
        self._purge_public_jobs()
        return list(self._public_jobs.values())

    def get_public_kwargs(self, job_id: str) -> dict[str, Any] | None:
        return self._public_kwargs.get(job_id)

    def update_public_params(self, job_id: str, patch: dict[str, Any]) -> PublicChatJob | None:
        job = self._public_jobs.get(job_id)
        if not job:
            return None
        job.params_summary = {**job.params_summary, **patch}
        job.touch()
        kw = self._public_kwargs.get(job_id)
        if kw is not None and "profile_id" in patch:
            kw["profile_id"] = patch["profile_id"]
        return job

    def set_public_profile(self, job_id: str, profile_id: str | None) -> PublicChatJob:
        job = self.get_public_job(job_id)
        if not job:
            raise KeyError("chatgpt_job_not_found")
        if job.status != "queued":
            raise ValueError("job_not_queued")
        pid = (profile_id or "").strip() or None
        job.params_summary["profile_id"] = pid
        job.params_summary["assigned_profile_id"] = pid
        job.params_summary["profile_assigned_by_user"] = bool(pid)
        job.touch()
        kw = self._public_kwargs.get(job_id)
        if kw is not None:
            kw["profile_id"] = pid
        return job

    def cancel_public_job(self, job_id: str) -> PublicChatJob:
        job = self.get_public_job(job_id)
        if not job:
            raise KeyError("chatgpt_job_not_found")
        if job.status in ("done", "failed", "cancelled"):
            return job
        task = self._public_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        job.status = "cancelled"
        job.error = "cancelled"
        job.touch()
        return job

    def list_public_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._purge_public_jobs()
        jobs = list(self._public_jobs.values())
        if status and status != "all":
            want = status.strip().lower()
            if want == "active":
                jobs = [j for j in jobs if j.status in ("queued", "running")]
            else:
                jobs = [j for j in jobs if j.status == want]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        limit = max(1, min(200, int(limit or 50)))
        return [j.to_dict(include_result=False) for j in jobs[:limit]]

    def mark_public_running(self, job_id: str) -> None:
        job = self._public_jobs.get(job_id)
        if not job:
            return
        job.status = "running"
        job.touch()

    def finish_public_job(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        job = self._public_jobs.get(job_id)
        if not job:
            return
        if job.status == "cancelled":
            self._public_tasks.pop(job_id, None)
            return
        if error:
            job.status = "failed"
            job.error = error
            job.result = result
        else:
            job.status = "done"
            job.error = None
            job.result = result or {}
        job.touch()
        self._public_tasks.pop(job_id, None)

    def track_public_task(self, job_id: str, task: asyncio.Task) -> None:
        self._public_tasks[job_id] = task

        def _done(t: asyncio.Task) -> None:
            self._public_tasks.pop(job_id, None)
            if t.cancelled():
                job = self._public_jobs.get(job_id)
                if job and job.status in ("queued", "running"):
                    job.status = "cancelled"
                    job.error = "cancelled"
                    job.touch()
            else:
                exc = t.exception()
                if exc is not None:
                    job = self._public_jobs.get(job_id)
                    if job and job.status in ("queued", "running"):
                        job.status = "failed"
                        job.error = str(exc)
                        job.touch()

        task.add_done_callback(_done)

    def stats(self) -> dict[str, Any]:
        self._purge_public_jobs()
        return {
            "pending_jobs": len(self._jobs),
            "queue_size": self._queue.qsize(),
            "workers_online": len(self.online_workers()),
            "workers": self.online_workers(),
            "public_jobs": len(self._public_jobs),
            "public_running": sum(
                1 for j in self._public_jobs.values() if j.status in ("queued", "running")
            ),
            "public_queued": sum(
                1 for j in self._public_jobs.values() if j.status == "queued"
            ),
        }


_broker: Optional[ChatgptBroker] = None


def get_chatgpt_broker() -> ChatgptBroker:
    global _broker
    if _broker is None:
        _broker = ChatgptBroker()
    return _broker
