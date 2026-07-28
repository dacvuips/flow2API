"""Captcha Broker — central pool cho reCAPTCHA Enterprise tokens.

Kiến trúc:
    Bridge (worker) --request_captcha--> Broker --long-poll--> Captcha Center
                          <---token----                 <--result--
Bridge chạy ở các Chrome profile khác (đăng nhập user).
Captcha Center chạy ở các Chrome profile riêng (labs.google/fx/tools/flow tabs)
— mỗi profile là 1 "center" độc lập, mint token qua grecaptcha.enterprise.execute.

Chính sách:
- Gắn cặp cố định Center ↔ Bridge (không chồng chéo): sort ổn định theo label/id,
  1:1 khi số lượng bằng nhau; bên thừa gắn hết vào phần tử cuối của bên ít hơn.
  VD 3 Center + 2 Bridge → C1↔B1, C2↔B2, C3↔B2.
- Chỉ gắn cặp Center online với Bridge đang nhận job VÀ sẵn sàng (ready),
  gồm cả offline-gen / Direct HTTP — không bắt buộc extension WS online.
- Trong nhóm center đã gắn với Bridge: LRU + skip cooldown.
- Khi có request: đánh thức long-poll các center gắn với Bridge đó, chờ sẵn sàng
  (cooldown / hard_reset / đăng ký lại sau agent restart) trước khi fail.
- Combined hard_reset trigger: mỗi 20 solve HOẶC mỗi 10 phút HOẶC khi worker
  báo API 403 (từ Google) — hard_reset ưu tiên center đã gắn với Bridge đó.
- Routing: mỗi request có commandId (uuid) — pending Future track theo commandId
  nên response chỉ resolve đúng caller (không lẫn profile Bridge).
- Dashboard có nút "Phân bổ lại" → prune Center offline + tính lại cặp.
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


def compute_fixed_pairs(
    center_ids: list[str],
    bridge_ids: list[str],
) -> list[tuple[str, str]]:
    """Gắn cặp cố định, không chồng chéo (contiguous overflow).

    - n == m → zip 1:1
    - n > m  → m-1 cặp đầu 1:1, mọi center còn lại gắn bridge cuối
    - m > n  → n-1 cặp đầu 1:1, mọi bridge còn lại gắn center cuối
    """
    n, m = len(center_ids), len(bridge_ids)
    if n == 0 or m == 0:
        return []
    pairs: list[tuple[str, str]] = []
    if n == m:
        for i in range(n):
            pairs.append((center_ids[i], bridge_ids[i]))
    elif n > m:
        for i in range(m - 1):
            pairs.append((center_ids[i], bridge_ids[i]))
        last_b = bridge_ids[m - 1]
        for i in range(m - 1, n):
            pairs.append((center_ids[i], last_b))
    else:
        for i in range(n - 1):
            pairs.append((center_ids[i], bridge_ids[i]))
        last_c = center_ids[n - 1]
        for i in range(n - 1, m):
            pairs.append((last_c, bridge_ids[i]))
    return pairs


# ── Timing constants (parity với veo3-captcha-extension) ────────────────
POLL_TIMEOUT_S = 25.0            # long-poll: server giữ tối đa 25s nếu không có command
CENTER_STALE_S = 45.0            # center không heartbeat quá 45s → offline
DEFAULT_REQUEST_TIMEOUT_S = 30.0 # broker.request_captcha total timeout
PICK_CENTER_MAX_WAIT_S = 12.0    # chờ center sẵn sàng khi cooldown / chưa poll lại
HARD_RESET_EVERY_N_SOLVES = 20   # combined trigger: N solves
HARD_RESET_EVERY_S = 600.0       # combined trigger: mỗi 10 phút
CENTER_COOLDOWN_AFTER_RESET_S = 5.0  # skip center 5s sau khi hard_reset hoàn tất
CENTER_STUCK_RESET_S = 90.0  # in_reset quá lâu → coi như kẹt, mở lại để nhận mint

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
        # hard_reset kẹt (extension crash / mất poll) → tự mở khóa sau CENTER_STUCK_RESET_S
        if self.in_reset:
            started = self.last_hard_reset_at or self.last_seen_at or self.connected_at
            if started and (now - started) > CENTER_STUCK_RESET_S:
                self.in_reset = False
                self.cooldown_until = min(self.cooldown_until, now)
                return now < self.cooldown_until
            return True
        return now < self.cooldown_until

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
            st.last_hard_reset_at = time.time()  # mốc để phát hiện reset kẹt
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
            st.last_seen_at = time.time()
            return out

        # Chờ event hoặc timeout
        try:
            await asyncio.wait_for(event.wait(), timeout=max(1.0, timeout))
        except asyncio.TimeoutError:
            st.last_seen_at = time.time()
            return []

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
        st.last_hard_reset_at = time.time()
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

    # ── Fixed Center ↔ Bridge pairing ───────────────────────────────────

    def _sorted_center_entries(self, *, online_only: bool = False) -> list[CenterState]:
        now = time.time()
        items = list(self._centers.values())
        if online_only:
            items = [s for s in items if s.is_online(now)]
        items.sort(key=lambda s: (str(s.label or "").lower(), s.center_id))
        return items

    def _sorted_bridge_entries(
        self,
        *,
        online_only: bool = False,
        dispatch_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Bridge (call API) profiles từ ExtensionPool — sort ổn định theo label/id."""
        try:
            from flow2api.services.extension_pool import get_extension_pool
            from flow2api.services.worker_settings import is_profile_dispatch_enabled

            sessions = get_extension_pool().list_sessions()
        except Exception:
            sessions = []
            is_profile_dispatch_enabled = None  # type: ignore[assignment]
        entries: list[dict[str, Any]] = []
        for s in sessions:
            pid = str(getattr(s, "profile_id", "") or "").strip()
            if not pid or pid.startswith("_"):
                continue
            try:
                label = str(s.display_name() if hasattr(s, "display_name") else "") or pid[:8]
            except Exception:
                label = pid[:8]
            online = bool(getattr(s, "connected", False))
            if online_only and not online:
                continue
            try:
                dispatch = bool(is_profile_dispatch_enabled(pid)) if is_profile_dispatch_enabled else True
            except Exception:
                dispatch = True
            if dispatch_only and not dispatch:
                continue
            entries.append({
                "profile_id": pid,
                "label": label,
                "online": online,
                "ready": bool(s.is_ready()) if hasattr(s, "is_ready") else False,
                "dispatch_enabled": dispatch,
            })
        entries.sort(key=lambda e: (str(e.get("label") or "").lower(), e["profile_id"]))
        return entries

    def prune_offline_centers(self) -> list[str]:
        """Xóa Center offline khỏi registry (ghost không còn heartbeat).

        Hủy pending request gắn với center bị prune. Trả về danh sách center_id đã xóa.
        """
        now = time.time()
        removed: list[str] = []
        for cid, st in list(self._centers.items()):
            if st.is_online(now):
                continue
            # Fail pending requests đang chờ center này
            for cmd_id, pending in list(self._pending.items()):
                if pending.center_id != cid:
                    continue
                self._pending.pop(cmd_id, None)
                fut = pending.future
                if not fut.done():
                    fut.set_exception(RuntimeError("CAPTCHA_CENTER_PRUNED"))
            self._centers.pop(cid, None)
            self._queues.pop(cid, None)
            self._events.pop(cid, None)
            removed.append(cid)
            logger.info("captcha-center pruned (offline): %s (label=%s)", cid[:12], st.label)
        return removed

    def unregister_center(self, center_id: str) -> dict[str, Any]:
        """Xóa Captcha Center khỏi dashboard (forget) — poll vẫn chạy ẩn đến khi quét lại."""
        cid = str(center_id or "").strip()
        if not cid:
            raise ValueError("invalid_center_id")
        from flow2api.services.worker_settings import forget_captcha_center

        st = self._centers.get(cid)
        was_online = bool(st and st.is_online())
        label = st.label if st else cid[:8]
        for cmd_id, pending in list(self._pending.items()):
            if pending.center_id != cid:
                continue
            self._pending.pop(cmd_id, None)
            fut = pending.future
            if not fut.done():
                fut.set_exception(RuntimeError("CAPTCHA_CENTER_REMOVED"))
        # Xóa queue command; giữ CenterState nếu đang online để Quét lại lấy ngay
        queue = self._queues.get(cid)
        if queue is not None:
            queue.clear()
        if not was_online:
            self._centers.pop(cid, None)
            self._queues.pop(cid, None)
            self._events.pop(cid, None)
        forget_captcha_center(cid)
        logger.info("captcha-center unregistered: %s (label=%s online=%s)", cid[:12], label, was_online)
        return {
            "ok": True,
            "center_id": cid,
            "label": label,
            "was_online": was_online,
        }

    def rediscover_online_centers(self) -> dict[str, Any]:
        """Lấy lại Captcha Center đang online đã bị xóa (forgotten)."""
        from flow2api.services.worker_settings import (
            get_worker_settings,
            unforget_captcha_center,
        )

        forgotten = list(get_worker_settings().captcha_center_forgotten)
        restored: list[str] = []
        now = time.time()
        for cid in forgotten:
            st = self._centers.get(cid)
            if st and st.is_online(now):
                unforget_captcha_center(cid)
                restored.append(cid)
        # Center đang poll nhưng chưa có trong _centers? Không xảy ra — poll luôn _ensure_center.
        # Center online mới (chưa forgotten) đã hiện sẵn.
        self.prune_offline_centers()
        return {
            "ok": True,
            "restored": restored,
            "restored_count": len(restored),
            "online_count": sum(1 for s in self._centers.values() if s.is_online(now)),
        }

    def redistribute(self) -> dict[str, Any]:
        """Phân bổ lại cặp Center ↔ Bridge: prune ghost + gắn lại (online + nhận job)."""
        pruned = self.prune_offline_centers()
        stats = self.stats()
        stats["pruned_center_ids"] = pruned
        stats["pruned_count"] = len(pruned)
        stats["redistributed"] = True
        logger.info(
            "captcha redistribute: pruned=%d online_centers=%d pairs=%d",
            len(pruned),
            stats.get("online_count", 0),
            len(stats.get("pairings") or []),
        )
        return stats

    def compute_pairings(self) -> dict[str, Any]:
        """Map Center ↔ Bridge cố định (không chồng chéo).

        Gắn cặp Center online với Bridge đang nhận job VÀ sẵn sàng chạy
        (ready — gồm cả offline-gen / Direct HTTP). Không bắt buộc WS online,
        vì gen vẫn cần reCAPTCHA khi Chrome đã tắt.
        """
        from flow2api.services.worker_settings import is_captcha_center_forgotten

        all_bridges = self._sorted_bridge_entries(online_only=False, dispatch_only=False)
        online_centers = [
            c for c in self._sorted_center_entries(online_only=True)
            if not is_captcha_center_forgotten(c.center_id)
        ]
        # Nhận job + ready (online WS hoặc offline-gen đều được)
        eligible_bridges = [
            b for b in all_bridges
            if b.get("dispatch_enabled") and b.get("ready")
        ]

        centers = online_centers
        bridges = eligible_bridges

        center_ids = [c.center_id for c in centers]
        bridge_ids = [b["profile_id"] for b in bridges]
        center_label = {c.center_id: c.label for c in centers}
        bridge_label = {b["profile_id"]: b["label"] for b in all_bridges}
        bridge_online = {b["profile_id"]: b["online"] for b in all_bridges}
        bridge_ready = {b["profile_id"]: b["ready"] for b in all_bridges}
        bridge_dispatch = {b["profile_id"]: b.get("dispatch_enabled", True) for b in all_bridges}
        now = time.time()

        raw_pairs = compute_fixed_pairs(center_ids, bridge_ids)
        pairs: list[dict[str, Any]] = []
        bridge_to_centers: dict[str, list[str]] = {b["profile_id"]: [] for b in all_bridges}
        for bid in bridge_ids:
            bridge_to_centers.setdefault(bid, [])
        center_to_bridges: dict[str, list[str]] = {cid: [] for cid in center_ids}

        for cid, bid in raw_pairs:
            if cid not in center_to_bridges:
                center_to_bridges[cid] = []
            if bid not in bridge_to_centers:
                bridge_to_centers[bid] = []
            if cid not in bridge_to_centers[bid]:
                bridge_to_centers[bid].append(cid)
            if bid not in center_to_bridges[cid]:
                center_to_bridges[cid].append(bid)
            pairs.append({
                "center_id": cid,
                "center_label": center_label.get(cid) or cid[:8],
                "center_online": bool(self._centers.get(cid) and self._centers[cid].is_online(now)),
                "bridge_profile_id": bid,
                "bridge_label": bridge_label.get(bid) or bid[:8],
                "bridge_online": bool(bridge_online.get(bid)),
                "bridge_ready": bool(bridge_ready.get(bid)),
                "bridge_dispatch_enabled": bool(bridge_dispatch.get(bid, True)),
            })

        return {
            "pairs": pairs,
            "bridge_to_centers": bridge_to_centers,
            "center_to_bridges": center_to_bridges,
            "center_count": len(center_ids),
            "bridge_count": len(bridge_ids),
        }

    def paired_center_ids_for_bridge(self, bridge_profile_id: str) -> list[str] | None:
        """Danh sách center gắn với Bridge. None = không giới hạn (mọi center online)."""
        bid = str(bridge_profile_id or "").strip()
        if not bid:
            return None
        pairing = self.compute_pairings()
        mapped = pairing["bridge_to_centers"].get(bid)
        if mapped:
            return list(mapped)
        # Bridge chưa nằm trong cặp (offline-gen / race) nhưng vẫn còn Center online
        # → không giới hạn, tránh NO_CAPTCHA_CENTER khi UI hiện 1 Center / 0 liên kết.
        if pairing.get("center_count"):
            return None
        if mapped is not None:
            return []
        return None

    # ── Center picker (paired + LRU + skip cooldown) ────────────────────

    def _wake_center_poll(self, center_id: str) -> None:
        """Đánh thức long-poll của center — trả command ngay thay vì chờ timeout."""
        ev = self._events.get(center_id)
        if ev is not None:
            ev.set()

    def _wake_online_centers(self, center_ids: set[str] | None = None) -> None:
        """Request mới → kick center online đang long-poll (lọc theo cặp nếu có)."""
        now = time.time()
        for st in self._centers.values():
            if center_ids is not None and st.center_id not in center_ids:
                continue
            if st.is_online(now):
                self._wake_center_poll(st.center_id)

    def _seconds_until_any_ready(
        self,
        now: float,
        center_ids: set[str] | None = None,
    ) -> float | None:
        """Giây chờ tới khi 1 center online thoát cooldown/in_reset; None nếu không có center."""
        if not self._centers:
            return None
        best: float | None = None
        any_matched = False
        for st in self._centers.values():
            if center_ids is not None and st.center_id not in center_ids:
                continue
            any_matched = True
            if not st.is_online(now):
                continue
            if not st.is_cooldown(now):
                return 0.0
            wait_s = max(0.0, st.cooldown_until - now) if now < st.cooldown_until else 0.0
            if st.in_reset:
                wait_s = max(wait_s, 1.0)
            if best is None or wait_s < best:
                best = wait_s
        if center_ids is not None and not any_matched:
            return None
        if best is not None:
            return best
        return None

    def pick_center(self, bridge_profile_id: str = "", *, allow_fallback: bool = True) -> str | None:
        now = time.time()
        from flow2api.services.worker_settings import is_captcha_center_forgotten

        preferred = self.paired_center_ids_for_bridge(bridge_profile_id)
        preferred_set = set(preferred) if preferred is not None else None

        def _collect(limit_to: set[str] | None) -> list[CenterState]:
            out: list[CenterState] = []
            for st in self._centers.values():
                if is_captcha_center_forgotten(st.center_id):
                    continue
                if limit_to is not None and st.center_id not in limit_to:
                    continue
                if not st.is_online(now):
                    continue
                if st.is_cooldown(now):
                    continue
                out.append(st)
            return out

        candidates = _collect(preferred_set)
        # Cặp gắn Center đang offline/cooldown → fallback mọi Center online còn lại
        if not candidates and allow_fallback and preferred_set is not None:
            candidates = _collect(None)
            if candidates:
                logger.warning(
                    "captcha pick fallback: bridge=%s paired=%s unavailable → using any online center",
                    (bridge_profile_id[:8] if bridge_profile_id else "-"),
                    sorted(preferred_set)[:3],
                )
        if not candidates:
            return None
        candidates.sort(key=lambda s: (s.last_mint_at, s.mint_count))
        return candidates[0].center_id

    async def _pick_center_for_request(
        self,
        wait_timeout: float,
        bridge_profile_id: str = "",
    ) -> str:
        """Chọn center sẵn sàng (theo cặp); nếu đang cooldown/chưa poll lại thì đánh thức + chờ."""
        preferred = self.paired_center_ids_for_bridge(bridge_profile_id)
        preferred_set = set(preferred) if preferred is not None else None
        deadline = time.time() + max(0.5, wait_timeout)
        # Đánh thức cặp trước; nếu rỗng thì đánh thức mọi center online
        wake_ids = preferred_set if preferred_set else None
        self._wake_online_centers(wake_ids)
        while True:
            now = time.time()
            center_id = self.pick_center(bridge_profile_id, allow_fallback=True)
            if center_id:
                return center_id
            remaining = deadline - now
            if remaining <= 0:
                break
            self._wake_online_centers(None)  # wake all while waiting
            until_ready = self._seconds_until_any_ready(now, preferred_set)
            if until_ready is None:
                until_ready = self._seconds_until_any_ready(now, None)
            if until_ready is None:
                sleep_s = min(0.5, remaining)
            elif until_ready <= 0:
                sleep_s = min(0.05, remaining)
            else:
                sleep_s = min(max(0.05, until_ready), 0.5, remaining)
            online_n = sum(1 for s in self._centers.values() if s.is_online(now))
            cooldown_n = sum(
                1 for s in self._centers.values()
                if s.is_online(now) and s.is_cooldown(now)
            )
            logger.info(
                "captcha pick waiting %.2fs (centers=%d online=%d cooldown=%d bridge=%s paired=%s)",
                sleep_s,
                len(self._centers),
                online_n,
                cooldown_n,
                (bridge_profile_id[:8] if bridge_profile_id else "-"),
                sorted(preferred_set) if preferred_set is not None else "all",
            )
            await asyncio.sleep(sleep_s)
        now = time.time()
        detail = (
            f"centers={len(self._centers)} "
            f"online={sum(1 for s in self._centers.values() if s.is_online(now))} "
            f"cooldown={sum(1 for s in self._centers.values() if s.is_online(now) and s.is_cooldown(now))} "
            f"paired={sorted(preferred_set) if preferred_set is not None else 'all'}"
        )
        logger.warning("NO_CAPTCHA_CENTER pick failed bridge=%s %s", bridge_profile_id[:8] if bridge_profile_id else "-", detail)
        raise RuntimeError(f"NO_CAPTCHA_CENTER ({detail})")

    # ── Public API: request captcha ─────────────────────────────────────

    async def request_captcha(
        self,
        action: str,
        bridge_profile_id: str = "",
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> str:
        """Blocking: gửi command cho center đã gắn với Bridge, chờ token."""
        if not action:
            raise ValueError("action_required")
        started = time.time()
        pick_budget = min(PICK_CENTER_MAX_WAIT_S, max(1.0, timeout * 0.4))
        center_id = await self._pick_center_for_request(pick_budget, bridge_profile_id)

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
        """Worker báo API 403 → force hard_reset cho center đã gắn với Bridge đó."""
        now = time.time()
        preferred = self.paired_center_ids_for_bridge(bridge_profile_id)
        preferred_set = set(preferred) if preferred is not None else None
        candidates = [s for s in self._centers.values() if s.is_online(now) and not s.in_reset]
        if preferred_set is not None:
            paired = [s for s in candidates if s.center_id in preferred_set]
            if paired:
                candidates = paired
            elif preferred_set:
                # Có cặp nhưng center đang offline/in_reset — không reset center lạ
                return None
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
        st.last_hard_reset_at = time.time()
        self._wake_center_poll(st.center_id)
        logger.info(
            "captcha-center %s: force hard_reset (reason=%s bridge=%s)",
            st.center_id[:12], reason, bridge_profile_id[:8] if bridge_profile_id else "-",
        )
        return st.center_id

    # ── Stats (dashboard / popup) ───────────────────────────────────────

    def stats(self) -> dict:
        now = time.time()
        from flow2api.services.worker_settings import is_captcha_center_forgotten

        pairing = self.compute_pairings()
        centers = []
        for st in self._centers.values():
            if is_captcha_center_forgotten(st.center_id):
                continue
            paired_bridges = pairing["center_to_bridges"].get(st.center_id) or []
            bridge_labels = []
            for bid in paired_bridges:
                label = next(
                    (p["bridge_label"] for p in pairing["pairs"] if p["bridge_profile_id"] == bid),
                    bid[:8],
                )
                bridge_labels.append(label)
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
                "paired_bridge_ids": list(paired_bridges),
                "paired_bridge_labels": bridge_labels,
            })
        centers.sort(key=lambda c: (not c["online"], c["label"]))
        return {
            "centers": centers,
            "online_count": sum(1 for c in centers if c["online"]),
            "pending_count": len(self._pending),
            "queued_count": sum(len(q) for q in self._queues.values()),
            "pairings": pairing["pairs"],
            "bridge_to_centers": pairing["bridge_to_centers"],
            "center_to_bridges": pairing["center_to_bridges"],
            "pairing_center_count": pairing["center_count"],
            "pairing_bridge_count": pairing["bridge_count"],
        }


_broker: Optional[CaptchaBroker] = None


def get_captcha_broker() -> CaptchaBroker:
    global _broker
    if _broker is None:
        _broker = CaptchaBroker()
    return _broker
