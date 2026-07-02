"""Doi CDP port san sang (dung sau launch-chrome-cdp)."""
from __future__ import annotations

import os
import sys


def main() -> int:
    from flow2api.services.system_ops import get_playwright_flow_cdp_port

    # Luon uu tien port tu Dashboard/config — tranh env he thong (vd. 9236) sai profile.
    port = get_playwright_flow_cdp_port()
    wait_port = os.environ.get("FLOW2API_CDP_WAIT_PORT", "").strip()
    if wait_port:
        try:
            port = int(wait_port)
        except ValueError:
            pass
    max_wait_s = int(os.environ.get("FLOW2API_CDP_WAIT_S", "120"))
    skip_relaunch = os.environ.get("FLOW2API_CDP_SKIP_RELAUNCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    from flow2api.services.playwright_pool import wait_cdp_port_blocking

    ready, host = wait_cdp_port_blocking(port, max_wait_s=max_wait_s, verbose=True)
    if ready:
        print(f"OK {host}:{port}", flush=True)
        return 0

    if skip_relaunch:
        return 1

    print("CDP chua len — thu mo lai Chrome (dong het instance cu)...", flush=True)
    from flow2api.services.system_ops import ensure_chrome_fully_closed, launch_flow_chrome_profile

    ensure_chrome_fully_closed()
    relaunch = launch_flow_chrome_profile(kill_chrome_first=False, wait_for_cdp=False)
    if not relaunch.get("ok"):
        print(relaunch.get("message") or relaunch, flush=True)
        return 1

    ready, host = wait_cdp_port_blocking(port, max_wait_s=max(60, max_wait_s // 2), verbose=True)
    if ready:
        print(f"OK {host}:{port} (sau relaunch)", flush=True)
        return 0
    print("Go y: KHONG mo Chrome bang icon desktop — chi dung launch-chrome-cdp.bat", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
