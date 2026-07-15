"""ChatGPT HTTP broker — dashboard → agent queue → extension poll (không cần Bridge WS)."""
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


class ChatgptBroker:
    def __init__(self) -> None:
        self._jobs: dict[str, ChatgptJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: dict[str, WorkerState] = {}
        self._waiters: list[asyncio.Future] = []

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

    def stats(self) -> dict[str, Any]:
        return {
            "pending_jobs": len(self._jobs),
            "queue_size": self._queue.qsize(),
            "workers_online": len(self.online_workers()),
            "workers": self.online_workers(),
        }


_broker: Optional[ChatgptBroker] = None


def get_chatgpt_broker() -> ChatgptBroker:
    global _broker
    if _broker is None:
        _broker = ChatgptBroker()
    return _broker
