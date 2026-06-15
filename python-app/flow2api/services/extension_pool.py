"""Multi Chrome profile pool — round-robin task dispatch across extension sessions."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import secrets
import time
import uuid
from typing import Any, Optional

from flow2api.services.api_trace import record_api_call
from flow2api.services.dashboard_events import events
from flow2api.services.request_logs import append_request_log

logger = logging.getLogger(__name__)

_bound_profile_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bound_profile_id", default=None
)
_trace_request_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "flow_trace_request_id", default=None
)


class ExtensionSession:
    """One Chrome profile's extension bridge (WS + Flow token)."""

    def __init__(self, profile_id: str, profile_label: str = "") -> None:
        self.profile_id = profile_id
        self.profile_label = profile_label or profile_id[:8]
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self.flow_key: Optional[str] = None
        self.token_captured_at: Optional[float] = None
        self.user_info: Optional[dict] = None
        self.paygate_tier: Optional[str] = None
        self._connected_at: Optional[float] = None
        self.active_jobs: int = 0
        self.assigned_total: int = 0

    @property
    def trace_request_id(self) -> Optional[str]:
        return _trace_request_id.get()

    @trace_request_id.setter
    def trace_request_id(self, value: Optional[str]) -> None:
        _trace_request_id.set(value)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def email(self) -> str:
        if isinstance(self.user_info, dict):
            return str(self.user_info.get("email") or "")
        return ""

    def display_name(self) -> str:
        if self.email:
            return self.email
        if self.profile_label and self.profile_label != self.profile_id[:8]:
            return self.profile_label
        return self.profile_id[:12]

    def is_ready(self) -> bool:
        return self.connected and bool(self.flow_key)

    def attach_ws(self, ws: Any) -> None:
        self._ws = ws
        self._connected_at = time.time()
        logger.info("Profile %s connected (%s)", self.profile_id[:12], self.display_name())

    def detach_ws(self) -> None:
        self._ws = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("Extension disconnected"))
        self._pending.clear()
        logger.warning("Profile %s disconnected (%s)", self.profile_id[:12], self.display_name())

    async def send_json(self, payload: dict) -> None:
        if self._ws:
            await self._ws.send(json.dumps(payload))

    async def handle_message(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "token_captured":
            self.flow_key = data.get("flowKey")
            self.token_captured_at = time.time()
            asyncio.create_task(self.fetch_paygate_tier())
            return
        if msg_type == "user_info":
            self.user_info = data.get("userInfo")
            if self.user_info and not self.profile_label:
                email = self.user_info.get("email")
                if email:
                    self.profile_label = str(email)
            return
        if msg_type in ("pong", "heartbeat"):
            return
        if msg_type == "extension_ready":
            label = data.get("profileLabel") or data.get("profile_label")
            if label:
                self.profile_label = str(label)
            if data.get("flowKeyPresent") and not self.flow_key:
                asyncio.create_task(self.fetch_paygate_tier())
            return

        req_id = data.get("id")
        if req_id and req_id in self._pending and not self._pending[req_id].done():
            self._pending[req_id].set_result(data)

    def resolve_callback(self, payload: dict) -> bool:
        req_id = payload.get("id")
        if req_id and req_id in self._pending and not self._pending[req_id].done():
            self._pending[req_id].set_result(payload)
            return True
        return False

    async def _send(self, method: str, params: dict, timeout: float = 120.0) -> dict:
        if not self._ws:
            raise RuntimeError("extension_not_connected")
        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps({"id": req_id, "method": method, "params": params}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("extension_timeout") from exc
        finally:
            self._pending.pop(req_id, None)

    async def api_request(
        self,
        url: str,
        method: str = "POST",
        headers: Optional[dict] = None,
        body: Any = None,
        captcha_action: Optional[str] = None,
        timeout: float = 180.0,
        *,
        raise_on_error: bool = True,
    ) -> dict:
        from flow2api.config import GOOGLE_FLOW_API

        params: dict[str, Any] = {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
        }
        if captcha_action:
            params["captchaAction"] = captcha_action
        resp = await self._send("api_request", params, timeout=timeout)
        if "aisandbox-pa.googleapis.com" in url:
            record_api_call(self.trace_request_id, url, method, body, resp)
        if not raise_on_error:
            return resp
        status = int(resp.get("status") or 0)
        if status >= 400:
            data = resp.get("data")
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                msg = data["error"].get("message") or data["error"].get("status")
                if msg:
                    raise RuntimeError(str(msg))
            err = resp.get("error") or data or f"HTTP_{status}"
            raise RuntimeError(str(err))
        return resp

    async def trpc_request(self, path: str, body: dict, timeout: float = 60.0) -> dict:
        from flow2api.config import TRPC_BASE

        url = f"{TRPC_BASE}/{path.lstrip('/')}"
        resp = await self._send(
            "trpc_request",
            {"url": url, "method": "POST", "headers": {"content-type": "application/json"}, "body": body},
            timeout=timeout,
        )
        status = int(resp.get("status") or 0)
        level = "error" if status >= 400 or resp.get("error") else "info"
        append_request_log(
            self.trace_request_id,
            "trpc",
            f"TRPC {path} → {status}",
            level=level,
            data={"url": url, "request_body": body, "response": resp.get("data") or resp},
        )
        if status >= 400:
            raise RuntimeError(resp.get("error") or f"TRPC_{status}")
        return resp.get("data") or resp

    async def get_media(self, media_id: str) -> dict:
        from flow2api.config import GOOGLE_API_KEY, GOOGLE_FLOW_API

        url = f"{GOOGLE_FLOW_API}/v1/media/{media_id}?key={GOOGLE_API_KEY}&clientContext.tool=PINHOLE"
        return await self.api_request(url, method="GET", timeout=60, raise_on_error=False)

    async def fetch_paygate_tier(self) -> Optional[str]:
        from flow2api.config import GOOGLE_FLOW_API

        if not self.flow_key:
            return None
        try:
            resp = await self.api_request(
                f"{GOOGLE_FLOW_API}/v1/credits",
                method="GET",
                headers={"accept": "application/json"},
                timeout=30,
            )
            data = resp.get("data") or {}
            tier = data.get("userPaygateTier")
            if tier:
                self.paygate_tier = tier
            return tier
        except Exception as exc:
            logger.warning("fetch_paygate_tier failed profile=%s: %s", self.profile_id[:8], exc)
            return None

    async def refresh_flow_token(self) -> bool:
        if not self._ws:
            return False
        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps({"type": "refresh_token", "id": req_id}))
        try:
            resp = await asyncio.wait_for(fut, timeout=15)
            return bool(resp.get("status") == 200 and resp.get("data", {}).get("ok"))
        except Exception:
            return False
        finally:
            self._pending.pop(req_id, None)

    def to_public_dict(self) -> dict[str, Any]:
        from flow2api.services.worker_settings import (
            get_profile_max_concurrent,
            is_profile_dispatch_enabled,
        )

        token_age = None
        if self.token_captured_at:
            token_age = int(time.time() - self.token_captured_at)
        max_c = get_profile_max_concurrent(self.profile_id)
        dispatch_enabled = is_profile_dispatch_enabled(self.profile_id)
        slots = max(0, max_c - self.active_jobs) if dispatch_enabled else 0
        return {
            "profile_id": self.profile_id,
            "profile_label": self.profile_label,
            "display_name": self.display_name(),
            "email": self.email,
            "online": self.connected,
            "ready": self.is_ready(),
            "dispatch_enabled": dispatch_enabled,
            "flow_key_present": bool(self.flow_key),
            "token_age_s": token_age,
            "paygate_tier": self.paygate_tier,
            "active_jobs": self.active_jobs,
            "max_concurrent": max_c,
            "slots_available": slots,
            "assigned_total": self.assigned_total,
            "user": self.user_info or {},
        }


class ExtensionPool:
    def __init__(self) -> None:
        self.callback_secret = secrets.token_urlsafe(24)
        self._sessions: dict[str, ExtensionSession] = {}
        self._ws_to_profile: dict[int, str] = {}
        self._rr_index: int = 0
        self._lock = asyncio.Lock()

    def bind_profile(self, profile_id: str) -> None:
        _bound_profile_id.set(profile_id)

    def unbind_profile(self) -> None:
        _bound_profile_id.set(None)

    def get_bound_id(self) -> Optional[str]:
        return _bound_profile_id.get()

    def get(self, profile_id: str) -> Optional[ExtensionSession]:
        return self._sessions.get(profile_id)

    def get_bound(self) -> Optional[ExtensionSession]:
        pid = _bound_profile_id.get()
        if pid:
            return self.get(pid)
        return None

    def first_ready(self) -> Optional[ExtensionSession]:
        ready = [s for s in self._sessions.values() if s.is_ready()]
        return ready[0] if ready else None

    def ready_sessions(self) -> list[ExtensionSession]:
        return [s for s in self._sessions.values() if s.is_ready()]

    def any_connected(self) -> bool:
        return any(s.connected for s in self._sessions.values())

    def online_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.connected)

    def ready_count(self) -> int:
        return len(self.ready_sessions())

    def list_public(self) -> list[dict[str, Any]]:
        items = [
            s.to_public_dict()
            for s in self._sessions.values()
            if not s.profile_id.startswith("_")
        ]
        items.sort(key=lambda x: (not x.get("ready"), x.get("display_name") or ""))
        return items

    async def register_ws(
        self,
        ws: Any,
        *,
        profile_id: str,
        profile_label: str = "",
    ) -> ExtensionSession:
        pid = (profile_id or "").strip() or f"auto-{uuid.uuid4().hex[:10]}"
        async with self._lock:
            session = self._sessions.get(pid)
            if not session:
                session = ExtensionSession(pid, profile_label)
                self._sessions[pid] = session
            elif profile_label:
                session.profile_label = profile_label
            session.attach_ws(ws)
            self._ws_to_profile[id(ws)] = pid
        await session.send_json({"type": "callback_secret", "secret": self.callback_secret})
        events.publish("profile_connected", {"profile_id": pid, "display_name": session.display_name()})
        append_request_log(
            None,
            "profile",
            f"Chrome profile connected: {session.display_name()}",
            level="info",
            data={"profile_id": pid},
        )
        return session

    async def unregister_ws(self, ws: Any) -> None:
        pid = self._ws_to_profile.pop(id(ws), None)
        if not pid:
            return
        session = self._sessions.get(pid)
        if session:
            session.detach_ws()
            events.publish("profile_disconnected", {"profile_id": pid})
            append_request_log(
                None,
                "profile",
                f"Chrome profile disconnected: {session.display_name()}",
                level="warn",
                data={"profile_id": pid},
            )

    async def handle_ws_message(self, ws: Any, data: dict) -> Optional[ExtensionSession]:
        pid = self._ws_to_profile.get(id(ws))
        if not pid:
            pid = str(data.get("profileId") or data.get("profile_id") or "").strip()
            label = str(data.get("profileLabel") or data.get("profile_label") or "")
            if pid or data.get("type") == "extension_ready":
                session = await self.register_ws(
                    ws,
                    profile_id=pid or f"auto-{uuid.uuid4().hex[:10]}",
                    profile_label=label,
                )
                await session.handle_message(data)
                return session
            return None
        session = self._sessions.get(pid)
        if session:
            await session.handle_message(data)
        return session

    def resolve_callback(self, payload: dict) -> bool:
        for session in self._sessions.values():
            if session.resolve_callback(payload):
                return True
        return False

    def _sessions_with_capacity(self, *, exclude: Optional[set[str]] = None) -> list[ExtensionSession]:
        from flow2api.services.worker_settings import (
            get_profile_max_concurrent,
            is_profile_dispatch_enabled,
        )

        out: list[ExtensionSession] = []
        for session in self.ready_sessions():
            if exclude and session.profile_id in exclude:
                continue
            if not is_profile_dispatch_enabled(session.profile_id):
                continue
            limit = get_profile_max_concurrent(session.profile_id)
            if session.active_jobs < limit:
                out.append(session)
        return out

    def has_available_profile(self) -> bool:
        return bool(self._sessions_with_capacity())

    def pick_round_robin(self, *, exclude: Optional[set[str]] = None) -> Optional[str]:
        ready = self._sessions_with_capacity(exclude=exclude)
        if not ready:
            return None
        ready.sort(key=lambda s: (s.active_jobs, s.assigned_total, s.profile_id))
        pick = ready[self._rr_index % len(ready)]
        self._rr_index = (self._rr_index + 1) % len(ready)
        pick.assigned_total += 1
        return pick.profile_id

    def pick_profile_for_retry(self, current_profile_id: str) -> Optional[str]:
        """Next profile in stable ring order; idle first; *current* only after full cycle."""
        from flow2api.services.worker_settings import (
            get_profile_max_concurrent,
            is_profile_dispatch_enabled,
        )

        current = (current_profile_id or "").strip()
        eligible: list[ExtensionSession] = []
        for session in self.ready_sessions():
            if session.profile_id.startswith("_"):
                continue
            if not is_profile_dispatch_enabled(session.profile_id):
                continue
            limit = get_profile_max_concurrent(session.profile_id)
            if session.active_jobs < limit:
                eligible.append(session)
        if not eligible:
            return None

        eligible.sort(key=lambda s: s.profile_id)
        ids = [s.profile_id for s in eligible]
        by_id = {s.profile_id: s for s in eligible}

        if current not in ids:
            idle = [s for s in eligible if s.active_jobs == 0]
            return (idle[0] if idle else eligible[0]).profile_id

        start = ids.index(current)
        # Offsets 1..n: walk the list in order; offset n returns to *current*.
        for offset in range(1, len(ids) + 1):
            pid = ids[(start + offset) % len(ids)]
            if by_id[pid].active_jobs == 0:
                return pid
        return ids[(start + 1) % len(ids)]

    def job_started(self, profile_id: str) -> None:
        session = self._sessions.get(profile_id)
        if session:
            session.active_jobs += 1

    def job_finished(self, profile_id: str) -> None:
        session = self._sessions.get(profile_id)
        if session and session.active_jobs > 0:
            session.active_jobs -= 1

    def reconcile_active_jobs(self, actual_by_profile: dict[str, int]) -> dict[str, dict[str, int]]:
        """Reset profile slot counters to match worker tasks actually running."""
        drift: dict[str, dict[str, int]] = {}
        for session in self._sessions.values():
            if session.profile_id.startswith("_"):
                continue
            actual = max(0, int(actual_by_profile.get(session.profile_id, 0)))
            if session.active_jobs != actual:
                drift[session.profile_id] = {
                    "reported": session.active_jobs,
                    "actual": actual,
                }
                session.active_jobs = actual
        return drift

    async def broadcast(self, payload: dict) -> None:
        for session in self._sessions.values():
            if session.profile_id.startswith("_"):
                continue
            if session.connected:
                try:
                    await session.send_json(payload)
                except Exception as exc:
                    logger.warning("broadcast to %s failed: %s", session.profile_id[:8], exc)

    def clear_all_credentials(self) -> None:
        for session in self._sessions.values():
            if session.profile_id.startswith("_"):
                continue
            session.user_info = None
            session.flow_key = None


_pool: Optional[ExtensionPool] = None


def get_extension_pool() -> ExtensionPool:
    global _pool
    if _pool is None:
        _pool = ExtensionPool()
    return _pool
