"""Captcha Broker — central pool cho reCAPTCHA Enterprise tokens.

Kiến trúc:
    Bridge (worker) --request_captcha--> Broker --long-poll--> Captcha Center
                          <---token----                 <--result--
Bridge chạy ở các Chrome profile khác (đăng nhập user).
Captcha Center chạy ở các Chrome profile riêng (labs.google/fx/tools/flow tabs)
— mỗi profile là 1 "center" độc lập, mint token qua grecaptcha.enterprise.execute.

Chính sách:
- LRU + skip cooldown khi chọn center để phục vụ 1 request.
- Khi có request: đánh thức long-poll mọi center online, chờ center sẵn sàng
  (cooldown / hard_reset / đăng ký lại sau agent restart) trước khi fail.
- Combined hard_reset trigger: mỗi 20 solve HOẶC mỗi 10 phút HOẶC khi worker
  báo API 403 (từ Google).
- Routing: mỗi request có commandId (uuid) — pending Future track theo commandId
  nên response chỉ resolve đúng caller (không lẫn profile Bridge).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from flow2api.config import STORAGE_DIR

logger = logging.getLogger(__name__)


# ── Timing constants (parity với veo3-captcha-extension) ────────────────
POLL_TIMEOUT_S = 25.0            # long-poll: server giữ tối đa 25s nếu không có command
CENTER_STALE_S = 45.0            # center không heartbeat quá 45s → offline
DEFAULT_REQUEST_TIMEOUT_S = 30.0 # broker.request_captcha total timeout
PICK_CENTER_MAX_WAIT_S = 12.0    # chờ center sẵn sàng khi cooldown / chưa poll lại
HARD_RESET_EVERY_N_SOLVES = 20   # combined trigger: N solves
HARD_RESET_EVERY_S = 600.0       # combined trigger: mỗi 10 phút
CENTER_COOLDOWN_AFTER_RESET_S = 5.0  # skip center 5s sau khi hard_reset hoàn tất

SECRET_FILE = STORAGE_DIR / "captcha-center.secret"


@dataclass
class CenterState:
    """Trạng thái 1 captcha-center (1 Chrome profile với tab Flow)."""

    center_id: str
    label: str = ""
    connected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    last_mint_at: float = 0.0
    last_mint_ok_at: float = 0.0
    last_hard_reset_at: float = 0.0
    last_soft_reset_at: float = 0.0
    mint_count: int = 0
    fail_count: int = 0
    solve_since_last_reset: int = 0
    in_reset: bool = False
    cooldown_until: float = 0.0
    version: str = ""

    def is_online(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_seen_at) < CENTER_STALE_S

    def is_cooldown(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self.in_reset or now < self.cooldown_until

    def needs_hard_reset(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if self.solve_since_last_reset >= HARD_RESET_EVERY_N_SOLVES:
            return True
        # Chỉ tính periodic nếu đã có ít nhất 1 mint (tránh reset ngay khi vừa attach)
        if self.last_mint_ok_at > 0 and (now - self.last_hard_reset_at) > HARD_RESET_EVERY_S:
            return True
        return False


@dataclass
class Command:
    """Command gửi cho captcha-center pull qua long-poll."""

    command_id: str
    method: str  # 'get_captcha' | 'soft_reset' | 'hard_reset'
    action: str = ""  # với get_captcha: pageAction (IMAGE_GENERATION / VIDEO_GENERATION / ...)
    bridge_profile_id: str = ""  # profile ID của Bridge đã yêu cầu (chỉ để log)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "commandId": self.command_id,
            "method": self.method,
            "action": self.action,
            "bridgeProfileId": self.bridge_profile_id,
        }


@dataclass
class PendingRequest:
    """Request đang chờ token từ 1 center cụ thể."""

    command_id: str
    action: str
    bridge_profile_id: str
    center_id: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)


def _load_or_create_secret() -> str:
    """Đọc secret từ file, sinh mới nếu chưa có. In-cleartext (local-only)."""
    try:
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        if SECRET_FILE.is_file():
            existing = SECRET_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        secret = secrets.token_urlsafe(32)
        SECRET_FILE.write_text(secret, encoding="utf-8")
        try:
            # Ghi log 1 lần để user copy sang Options của extension
            logger.info(
                "captcha-center secret sinh mới → %s (dán vào Options của extension mode 'Captcha Center')",
                SECRET_FILE,
            )
        except Exception:
            pass
        return secret
    except Exception as exc:
        logger.warning("captcha-center secret file failed (%s) — dùng secret in-memory", exc)
        return secrets.token_urlsafe(32)


class CaptchaBroker:
    """Central broker cho tokens.

    Thread-safety: dùng trong asyncio single-thread — không cần lock ngoài
    `_pending` / `_queues` được truy cập từ nhiều coroutine.
    """

    def __init__(self) -> None:
        self._secret: str = _load_or_create_secret()
        self._centers: dict[str, CenterState] = {}
        # Mỗi center có 1 queue command riêng và 1 Event để wake long-poll.
        self._queues: dict[str, deque[Command]] = {}
        self._events: dict[str, asyncio.Event] = {}
        # commandId -> PendingRequest.
        self._pending: dict[str, PendingRequest] = {}
        # bridge profile_id -> deque[float] các timestamp reject 403 gần đây
        # (dùng để trigger hard_reset trên center đã cấp token này).
        self._rr_index: int = 0

    # ── Auth ────────────────────────────────────────────────────────────

    @property
    def secret(self) -> str:
        return self._secret

    def check_secret(self, provided: str | None) -> bool:
        return bool(provided) and bool(self._secret) and secrets.compare_digest(provided, self._secret)

    # ── Center registry ─────────────────────────────────────────────────

    def _ensure_center(self, center_id: str, label: str = "", version: str = "") -> CenterState:
        st = self._centers.get(center_id)
        if not st:
            st = CenterState(center_id=center_id, label=label or center_id[:8], version=version)
            self._centers[center_id] = st
            self._queues[center_id] = deque()
            self._events[center_id] = asyncio.Event()
            logger.info("captcha-center registered: %s (label=%s)", center_id[:12], st.label)
        else:
            st.last_seen_at = time.time()
            if label:
                st.label = label
            if version:
                st.version = version
        return st

    def submit_event(self, center_id: str, event_type: str, payload: dict | None = None) -> None:
        """Center báo sự kiện: heartbeat, extension_ready, anchor_clear, mint_done."""
        payload = payload or {}
        label = str(payload.get("label") or "")
        version = str(payload.get("version") or "")
        st = self._ensure_center(center_id, label=label, version=version)
        st.last_seen_at = time.time()
        # heartbeat: chỉ update last_seen_at (đã làm ở trên)
        if event_type == "hard_reset_started":
            st.in_reset = True
            st.solve_since_last_reset = 0
        elif event_type == "hard_reset_finished":
            st.in_reset = False
            st.last_hard_reset_at = time.time()
            st.cooldown_until = time.time() + CENTER_COOLDOWN_AFTER_RESET_S
        elif event_type == "soft_reset_finished":
            st.last_soft_reset_at = time.time()

    # ── Long-poll ───────────────────────────────────────────────────────

    async def poll(self, center_id: str, label: str = "", version: str = "", timeout: float = POLL_TIMEOUT_S) -> list[dict]:
        """Long-poll: chờ tối đa `timeout` s cho command mới cho center này.

        Response format:
            {"commands": [...]}  # có thể rỗng
        """
        st = self._ensure_center(center_id, label=label, version=version)
        queue = self._queues[center_id]
        event = self._events[center_id]

        # Kick trigger hard_reset periodic ngay khi poll → nếu cần, enqueue vào queue trước khi wait
        self._maybe_enqueue_periodic_reset(st)

        # Nếu đã có command sẵn → trả về ngay
        if queue:
            out = [cmd.to_dict() for cmd in list(queue)]
            queue.clear()
            event.clear()
            return out

        # Chờ event hoặc timeout
        try:
            await asyncio.wait_for(event.wait(), timeout=max(1.0, timeout))
        except asyncio.TimeoutError:
            return []
        finally:
            # Luôn re-fetch queue vì event có thể được set bởi request khác
            pass

        out = [cmd.to_dict() for cmd in list(queue)]
        queue.clear()
        event.clear()
        st.last_seen_at = time.time()
        return out

    def _maybe_enqueue_periodic_reset(self, st: CenterState) -> None:
        if st.in_reset:
            return
        if not st.needs_hard_reset():
            return
        # Chỉ enqueue nếu chưa có hard_reset trong queue (tránh duplicate)
        queue = self._queues.get(st.center_id)
        if queue is None:
            return
        for cmd in queue:
            if cmd.method == "hard_reset":
                return
        cmd = Command(command_id=f"periodic-{uuid.uuid4().hex[:8]}", method="hard_reset")
        queue.append(cmd)
        st.in_reset = True
        self._wake_center_poll(st.center_id)
        logger.info(
            "captcha-center %s: enqueue periodic hard_reset (solves=%d, age=%.0fs)",
            st.center_id[:12], st.solve_since_last_reset, time.time() - st.last_hard_reset_at,
        )

    # ── Result submit ───────────────────────────────────────────────────

    def submit_result(self, command_id: str, token: str | None, error: str | None, center_id: str | None = None) -> bool:
        """Center POST kết quả về. Resolve pending future."""
        pending = self._pending.pop(command_id, None)
        # Update center state (mint count) even nếu no pending (VD periodic reset)
        cid = center_id or (pending.center_id if pending else "")
        if cid:
            st = self._centers.get(cid)
            if st:
                now = time.time()
                st.last_seen_at = now
                if command_id.startswith("periodic-"):
                    # Trả kết quả của periodic hard_reset
                    if token in ("hard_reset_ok", "soft_reset_ok"):
                        st.in_reset = False
                        st.last_hard_reset_at = now
                        st.cooldown_until = now + CENTER_COOLDOWN_AFTER_RESET_S
                    return True
                if token:
                    st.mint_count += 1
                    st.last_mint_ok_at = now
                    st.last_mint_at = now
                    st.solve_since_last_reset += 1
                else:
                    st.fail_count += 1
                    st.last_mint_at = now
        if not pending:
            return False
        fut = pending.future
        if fut.done():
            return False
        if token:
            fut.set_result(token)
        else:
            fut.set_exception(RuntimeError(error or "CAPTCHA_FAILED"))
        return True

    # ── Center picker (LRU + skip cooldown) ─────────────────────────────

    def _wake_center_poll(self, center_id: str) -> None:
        """Đánh thức long-poll của center — trả command ngay thay vì chờ timeout."""
        ev = self._events.get(center_id)
        if ev is not None:
            ev.set()

    def _wake_online_centers(self) -> None:
        """Request mới → kick mọi center online đang long-poll."""
        now = time.time()
        for st in self._centers.values():
            if st.is_online(now):
                self._wake_center_poll(st.center_id)

    def _seconds_until_any_ready(self, now: float) -> float | None:
        """Giây chờ tới khi 1 center online thoát cooldown/in_reset; None nếu không có center."""
        if not self._centers:
            return None
        best: float | None = None
        for st in self._centers.values():
            if not st.is_online(now):
                continue
            if not st.is_cooldown(now):
                return 0.0
            wait_s = max(0.0, st.cooldown_until - now) if now < st.cooldown_until else 0.0
            if st.in_reset:
                wait_s = max(wait_s, 1.0)
            if best is None or wait_s < best:
                best = wait_s
        if best is not None:
            return best
        return None

    def pick_center(self) -> str | None:
        now = time.time()
        candidates: list[CenterState] = []
        for st in self._centers.values():
            if not st.is_online(now):
                continue
            if st.is_cooldown(now):
                continue
            candidates.append(st)
        if not candidates:
            return None
        # LRU: last_mint_at nhỏ nhất → chọn trước (chưa mint => 0 => ưu tiên cao nhất)
        candidates.sort(key=lambda s: (s.last_mint_at, s.mint_count))
        return candidates[0].center_id

    async def _pick_center_for_request(self, wait_timeout: float) -> str:
        """Chọn center sẵn sàng; nếu đang cooldown/chưa poll lại thì đánh thức + chờ."""
        deadline = time.time() + max(0.5, wait_timeout)
        self._wake_online_centers()
        while True:
            now = time.time()
            center_id = self.pick_center()
            if center_id:
                return center_id
            remaining = deadline - now
            if remaining <= 0:
                break
            self._wake_online_centers()
            until_ready = self._seconds_until_any_ready(now)
            if until_ready is None:
                sleep_s = min(0.5, remaining)
            elif until_ready <= 0:
                sleep_s = min(0.05, remaining)
            else:
                sleep_s = min(max(0.05, until_ready), 0.5, remaining)
            logger.debug(
                "captcha pick waiting %.2fs (centers=%d online=%d)",
                sleep_s,
                len(self._centers),
                sum(1 for s in self._centers.values() if s.is_online(now)),
            )
            await asyncio.sleep(sleep_s)
        raise RuntimeError("NO_CAPTCHA_CENTER")

    # ── Public API: request captcha ─────────────────────────────────────

    async def request_captcha(
        self,
        action: str,
        bridge_profile_id: str = "",
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> str:
        """Blocking: gửi command cho center, chờ token. Raise RuntimeError khi fail."""
        if not action:
            raise ValueError("action_required")
        started = time.time()
        pick_budget = min(PICK_CENTER_MAX_WAIT_S, max(1.0, timeout * 0.4))
        center_id = await self._pick_center_for_request(pick_budget)

        command_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = PendingRequest(
            command_id=command_id,
            action=action,
            bridge_profile_id=bridge_profile_id or "",
            center_id=center_id,
            future=fut,
        )
        self._pending[command_id] = pending

        cmd = Command(
            command_id=command_id,
            method="get_captcha",
            action=action,
            bridge_profile_id=bridge_profile_id or "",
        )
        self._queues[center_id].append(cmd)
        self._wake_center_poll(center_id)

        logger.debug(
            "captcha request %s: action=%s center=%s bridge=%s",
            command_id[:8], action, center_id[:12], bridge_profile_id[:8] if bridge_profile_id else "-",
        )

        mint_timeout = max(5.0, timeout - (time.time() - started))
        try:
            token = await asyncio.wait_for(fut, timeout=mint_timeout)
            return token
        except asyncio.TimeoutError:
            self._pending.pop(command_id, None)
            raise RuntimeError("CAPTCHA_BROKER_TIMEOUT")
        finally:
            self._pending.pop(command_id, None)

    def request_hard_reset(self, bridge_profile_id: str = "", reason: str = "manual") -> str | None:
        """Worker báo API 403 → force hard_reset cho center đã mint (nếu track được).

        Hiện tại đơn giản: chọn 1 center online-not-in-reset và enqueue hard_reset.
        """
        # Chọn center: prefer center vừa mint nhất (last_mint_at gần nhất)
        now = time.time()
        candidates = [s for s in self._centers.values() if s.is_online(now) and not s.in_reset]
        if not candidates:
            return None
        candidates.sort(key=lambda s: -s.last_mint_at)
        st = candidates[0]
        queue = self._queues.get(st.center_id)
        if queue is None:
            return None
        cmd = Command(command_id=f"periodic-{uuid.uuid4().hex[:8]}", method="hard_reset")
        queue.append(cmd)
        st.in_reset = True
        self._wake_center_poll(st.center_id)
        logger.info(
            "captcha-center %s: force hard_reset (reason=%s bridge=%s)",
            st.center_id[:12], reason, bridge_profile_id[:8] if bridge_profile_id else "-",
        )
        return st.center_id

    # ── Stats (dashboard / popup) ───────────────────────────────────────

    def stats(self) -> dict:
        now = time.time()
        centers = []
        for st in self._centers.values():
            centers.append({
                "center_id": st.center_id,
                "label": st.label,
                "online": st.is_online(now),
                "in_reset": st.in_reset,
                "in_cooldown": st.is_cooldown(now),
                "mint_count": st.mint_count,
                "fail_count": st.fail_count,
                "solve_since_last_reset": st.solve_since_last_reset,
                "last_mint_age_s": int(now - st.last_mint_at) if st.last_mint_at else None,
                "last_hard_reset_age_s": int(now - st.last_hard_reset_at) if st.last_hard_reset_at else None,
                "last_seen_age_s": int(now - st.last_seen_at) if st.last_seen_at else None,
                "version": st.version,
            })
        centers.sort(key=lambda c: (not c["online"], c["label"]))
        return {
            "centers": centers,
            "online_count": sum(1 for c in centers if c["online"]),
            "pending_count": len(self._pending),
            "queued_count": sum(len(q) for q in self._queues.values()),
        }


_broker: Optional[CaptchaBroker] = None


def get_captcha_broker() -> CaptchaBroker:
    global _broker
    if _broker is None:
        _broker = CaptchaBroker()
    return _broker
