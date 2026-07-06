"""Playwright pool — one CDP-backed browser session per connected Chrome profile."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from flow2api.config import (
    CDP_AUTO_LAUNCH,
    CDP_AUTO_LAUNCH_WAIT_S,
    CDP_BASE_PORT,
    CDP_CONNECT_TIMEOUT_S,
    CDP_PROBE_HOSTS,
    CDP_PROBE_RETRIES,
    PLAYWRIGHT_ENABLED,
)

logger = logging.getLogger(__name__)

_pool: Optional["PlaywrightPool"] = None
_cdp_launch_lock = asyncio.Lock()


def is_playwright_enabled() -> bool:
    return PLAYWRIGHT_ENABLED


def _cdp_scan_count() -> int:
    from flow2api.services.system_ops import sorted_chrome_profiles

    return max(32, len(sorted_chrome_profiles()) + 8)


def _profile_cdp_port(profile_id: str) -> int:
    from flow2api.services.system_ops import get_cdp_port_for_extension_profile

    return get_cdp_port_for_extension_profile(profile_id)


def _cdp_version_url(port: int, host: str) -> str:
    return f"http://{host}:{port}/json/version"


def cdp_check_url(port: int, host: str | None = None) -> str:
    h = host or (CDP_PROBE_HOSTS[0] if CDP_PROBE_HOSTS else "localhost")
    return _cdp_version_url(port, h)


async def probe_cdp_port(
    port: int,
    *,
    timeout: float = 2.0,
    host: str | None = None,
) -> dict[str, Any] | None:
    hosts = (host,) if host else CDP_PROBE_HOSTS
    for h in hosts:
        url = _cdp_version_url(port, h)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    base = data if isinstance(data, dict) else {"raw": data}
                    return {**base, "cdp_host": h, "cdp_url": url}
        except Exception:
            continue
    return None


def wait_cdp_port_blocking(
    port: int,
    *,
    max_wait_s: int = 120,
    step_s: float = 3.0,
    verbose: bool = False,
) -> tuple[bool, str]:
    """Chờ CDP (sync) — thử localhost rồi 127.0.0.1."""
    import sys
    import time

    tries = max(1, int(max_wait_s / step_s))
    if verbose:
        print(f"  Doi CDP port {port} (toi da {max_wait_s}s)...", flush=True)
    for i in range(tries):
        for h in CDP_PROBE_HOSTS:
            url = _cdp_version_url(port, h)
            try:
                with httpx.Client(timeout=2.5) as client:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        if verbose:
                            print(f"  OK {h}:{port} (sau {int(i * step_s)}s)", flush=True)
                        elif i:
                            logger.info("CDP port %s ready on %s (sau %ss)", port, h, int(i * step_s))
                        return True, h
            except Exception:
                continue
        elapsed = int((i + 1) * step_s)
        if verbose and elapsed % 15 == 0:
            try:
                from flow2api.services.system_ops import diagnose_chrome_cdp

                print(f"  Ghi chu: {diagnose_chrome_cdp(port)}", flush=True)
            except Exception:
                pass
        if verbose:
            print(f"  ... chua thay CDP ({elapsed}s / {max_wait_s}s)", flush=True)
        elif i and i % 5 == 0:
            logger.info("Still waiting CDP port %s (%ss)", port, elapsed)
        time.sleep(step_s)
    if verbose:
        print(f"  TIMEOUT — thu http://localhost:{port}/json/version", flush=True)
        try:
            from flow2api.services.system_ops import diagnose_chrome_cdp

            print(f"  {diagnose_chrome_cdp(port)}", flush=True)
        except Exception:
            pass
    return False, ""


async def resolve_cdp_endpoint(port: int) -> str:
    """URL Playwright connect_over_cdp — dùng host probe được."""
    result = await probe_cdp_port(port, timeout=2.0)
    if result and result.get("cdp_host"):
        return f"http://{result['cdp_host']}:{port}"
    host = CDP_PROBE_HOSTS[0] if CDP_PROBE_HOSTS else "localhost"
    return f"http://{host}:{port}"


async def list_active_cdp_ports(
    base_port: int | None = None,
    *,
    count: int | None = None,
) -> list[int]:
    base = int(base_port if base_port is not None else CDP_BASE_PORT)
    span = count if count is not None else _cdp_scan_count()
    ports: list[int] = []
    for port in range(base, base + max(1, span)):
        if await probe_cdp_port(port, timeout=0.8):
            ports.append(port)
    return ports


def _cdp_unavailable_message(port: int, active: list[int], *, profile_id: str = "") -> str:
    from flow2api.services.extension_pool import get_extension_pool

    scan_end = CDP_BASE_PORT + _cdp_scan_count() - 1
    email = ""
    chrome_dir = ""
    if profile_id:
        session = get_extension_pool().get(profile_id)
        if session:
            email = str(session.email or "")
        from flow2api.services.system_ops import email_to_chrome_dir

        if email:
            chrome_dir = email_to_chrome_dir(email) or ""
    lines = [
        f"Chrome CDP không phản hồi tại port {port}.",
    ]
    if email:
        lines.append(f"Profile extension: {email}.")
    if chrome_dir:
        lines.append(f"Chrome profile: {chrome_dir}.")
    lines.extend(
        [
            "Mở Chrome bằng Dashboard → Mở profile Flow (hoặc launch-chrome-cdp.bat).",
            f"Kiểm tra: http://localhost:{port}/json/version hoặc http://127.0.0.1:{port}/json/version",
        ]
    )
    if active:
        lines.append(f"CDP đang mở tại: {', '.join(str(p) for p in active)}")
        if port not in active:
            lines.append(
                f"Có thể sai port — set FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT={active[0]}"
            )
    else:
        lines.append(f"Không thấy CDP nào ({CDP_BASE_PORT}–{scan_end}). Chrome chưa có --remote-debugging-port.")
    return " ".join(lines)


async def _wait_for_cdp_port(port: int, *, max_wait_s: int | None = None) -> bool:
    wait_s = max(10, int(max_wait_s if max_wait_s is not None else CDP_AUTO_LAUNCH_WAIT_S))
    step_s = 3
    tries = max(1, wait_s // step_s)
    for attempt in range(tries):
        if await probe_cdp_port(port, timeout=2.0):
            if attempt:
                logger.info("CDP port %s ready after auto-launch (%ss)", port, attempt * step_s)
            return True
        await asyncio.sleep(step_s)
    return False


async def _auto_launch_cdp_for_session(session: "PlaywrightProfileSession", port: int) -> bool:
    if not CDP_AUTO_LAUNCH:
        return False
    async with _cdp_launch_lock:
        if await probe_cdp_port(port, timeout=1.5):
            return True
        from flow2api.services.system_ops import launch_flow_chrome_for_extension

        logger.warning(
            "CDP port %s chua mo — tu dong mo Chrome CDP profile=%s (giu Chromium extension)",
            port,
            session.profile_id[:12],
        )
        result = await asyncio.to_thread(
            launch_flow_chrome_for_extension,
            session.profile_id,
            kill_chrome_first=False,
            wait_for_cdp=True,
        )
        if not result.get("ok"):
            logger.error("Auto-launch Chrome CDP failed: %s", result.get("message") or result)
            return False
        logger.info("Auto-launch Chrome: %s", result.get("message") or result)
        return await _wait_for_cdp_port(port)


async def _resolve_cdp_port(session: "PlaywrightProfileSession") -> int:
    """CDP port theo email Chrome profile của extension (tự động map)."""
    flow_port = _profile_cdp_port(session.profile_id)
    session.cdp_port = flow_port
    retries = max(1, CDP_PROBE_RETRIES)
    for attempt in range(retries):
        if await probe_cdp_port(flow_port, timeout=2.0):
            if attempt:
                logger.info(
                    "CDP port %s ready after %s probe(s) profile=%s",
                    flow_port,
                    attempt + 1,
                    session.profile_id[:12],
                )
            return flow_port
        if attempt + 1 < retries:
            await asyncio.sleep(2.0)

    if await _auto_launch_cdp_for_session(session, flow_port):
        return flow_port

    active = await list_active_cdp_ports()
    raise RuntimeError(_cdp_unavailable_message(flow_port, active, profile_id=session.profile_id))


class PlaywrightProfileSession:
    """Playwright page attached to a Chrome profile via CDP."""

    def __init__(self, profile_id: str, cdp_port: int) -> None:
        self.profile_id = profile_id
        self.cdp_port = cdp_port
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._proxy_display: str = ""
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._page is not None

    async def disconnect(self) -> None:
        async with self._lock:
            self._page = None
            # CDP attach: chỉ ngắt Playwright, không đóng Chrome của user.
            self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.debug("playwright stop %s: %s", self.profile_id[:8], exc)
            self._playwright = None

    async def sync_proxy(self, *, force: bool = True) -> dict[str, Any]:
        """
        Đồng bộ proxy pool → Chrome extension trước UI automation.
        CDP attach dùng chung Chrome nên mọi request Playwright đi qua proxy đã gắn.
        """
        from flow2api.services.system_ops import ensure_profile_proxy_applied

        info = await ensure_profile_proxy_applied(self.profile_id, force=force)
        display = str(info.get("proxy_display") or info.get("proxy") or "")
        if self._proxy_display and display != self._proxy_display and self.connected:
            logger.info(
                "Playwright proxy changed profile=%s %s -> %s, reconnect",
                self.profile_id[:12],
                self._proxy_display,
                display or "(direct)",
            )
            await self.disconnect()
        self._proxy_display = display
        return info

    async def get_page(
        self,
        *,
        flow_url_hint: str = "",
        sync_proxy: bool = True,
        force_proxy: bool = True,
    ) -> Any:
        async with self._lock:
            if sync_proxy:
                await self.sync_proxy(force=force_proxy)
            if self._page and not self._page.is_closed():
                return self._page
            await self._connect(flow_url_hint=flow_url_hint)
            if not self._page:
                raise RuntimeError(f"playwright_no_page profile={self.profile_id[:12]}")
            return self._page

    async def _connect(self, *, flow_url_hint: str = "") -> None:
        from playwright.async_api import async_playwright

        port = await _resolve_cdp_port(self)
        endpoint = await resolve_cdp_endpoint(port)
        logger.info(
            "Playwright CDP connect profile=%s flow_port=%s proxy=%s",
            self.profile_id[:12],
            port,
            self._proxy_display or "direct",
        )

        self._playwright = await async_playwright().start()
        timeout_ms = int(CDP_CONNECT_TIMEOUT_S * 1000)
        try:
            self._browser = await asyncio.wait_for(
                self._playwright.chromium.connect_over_cdp(endpoint, timeout=timeout_ms),
                timeout=CDP_CONNECT_TIMEOUT_S + 5,
            )
        except Exception as exc:
            await self.disconnect()
            active = await list_active_cdp_ports()
            hint = _cdp_unavailable_message(port, active, profile_id=self.profile_id)
            raise RuntimeError(f"playwright_cdp_failed port={port}: {exc}. {hint}") from exc

        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError(f"playwright_no_context port={self.cdp_port}")

        pages: list[Any] = []
        for ctx in contexts:
            pages.extend(ctx.pages)

        flow_page = _pick_flow_page(pages, flow_url_hint)
        if flow_page is None and pages:
            flow_page = pages[0]
        if flow_page is None:
            ctx = contexts[0]
            if flow_url_hint:
                flow_page = await ctx.new_page()
                await flow_page.goto(flow_url_hint, wait_until="domcontentloaded", timeout=60000)
            else:
                raise RuntimeError(f"playwright_no_flow_tab port={self.cdp_port}")

        self._page = flow_page
        logger.info(
            "Playwright attached profile=%s url=%s",
            self.profile_id[:12],
            (self._page.url or "")[:80],
        )


def _pick_flow_page(pages: list[Any], flow_url_hint: str) -> Any | None:
    hint = (flow_url_hint or "").strip().lower()
    for page in pages:
        url = (page.url or "").lower()
        if "labs.google" in url and "flow" in url:
            return page
    if hint:
        for page in pages:
            if hint.split("/fx")[0] and hint.split("/fx")[0] in (page.url or "").lower():
                return page
    return None


class PlaywrightPool:
    def __init__(self) -> None:
        self._sessions: dict[str, PlaywrightProfileSession] = {}
        self._lock = asyncio.Lock()

    def assign_cdp_port(self, profile_id: str) -> int:
        """Mỗi extension profile dùng CDP port map theo email Chrome profile."""
        return _profile_cdp_port(profile_id)

    async def on_profile_connected(self, profile_id: str) -> None:
        if not PLAYWRIGHT_ENABLED:
            return
        pid = str(profile_id or "").strip()
        if not pid or pid.startswith("_"):
            return
        port = self.assign_cdp_port(pid)
        async with self._lock:
            if pid not in self._sessions:
                self._sessions[pid] = PlaywrightProfileSession(pid, port)
        from flow2api.services.system_ops import email_to_chrome_dir
        from flow2api.services.extension_pool import get_extension_pool

        ext = get_extension_pool().get(pid)
        email = str(ext.email or "") if ext else ""
        chrome_dir = email_to_chrome_dir(email) if email else ""
        logger.info(
            "Playwright slot profile=%s email=%s chrome=%s cdp_port=%s",
            pid[:12],
            email or "?",
            chrome_dir or "?",
            port,
        )
        if CDP_AUTO_LAUNCH:
            asyncio.create_task(self._auto_launch_cdp_on_connect(pid, port))

    async def _auto_launch_cdp_on_connect(self, profile_id: str, port: int) -> None:
        if await probe_cdp_port(port, timeout=1.5):
            return
        async with _cdp_launch_lock:
            if await probe_cdp_port(port, timeout=1.0):
                return
            from flow2api.services.system_ops import launch_flow_chrome_for_extension

            logger.info(
                "Extension ket noi — tu mo CDP port %s cho profile=%s",
                port,
                profile_id[:12],
            )
            result = await asyncio.to_thread(
                launch_flow_chrome_for_extension,
                profile_id,
                kill_chrome_first=False,
                wait_for_cdp=False,
            )
            if not result.get("ok"):
                logger.warning(
                    "Auto CDP on connect failed profile=%s: %s",
                    profile_id[:12],
                    result.get("message") or result,
                )

    async def on_profile_disconnected(self, profile_id: str) -> None:
        pid = str(profile_id or "").strip()
        session = self._sessions.pop(pid, None)
        if session:
            await session.disconnect()

    async def get_session(self, profile_id: str) -> PlaywrightProfileSession:
        pid = str(profile_id or "").strip()
        if not pid:
            raise ValueError("missing_profile_id")
        async with self._lock:
            session = self._sessions.get(pid)
            if not session:
                port = self.assign_cdp_port(pid)
                session = PlaywrightProfileSession(pid, port)
                self._sessions[pid] = session
        return session

    async def invalidate_profile(self, profile_id: str) -> None:
        pid = str(profile_id or "").strip()
        if not pid:
            return
        session = self._sessions.get(pid)
        if session:
            await session.disconnect()

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.disconnect()


def get_playwright_pool() -> PlaywrightPool:
    global _pool
    if _pool is None:
        _pool = PlaywrightPool()
    return _pool


async def shutdown_playwright_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.shutdown()
        _pool = None
