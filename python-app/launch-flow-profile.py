"""Mo Chrome profile Flow + CDP (buoc 3 cua launch-chrome-cdp.bat)."""
from __future__ import annotations

import sys


def main() -> int:
    from flow2api.services.system_ops import (
        diagnose_chrome_cdp,
        ensure_flow_launch_script,
        get_playwright_flow_cdp_port,
        get_playwright_flow_chrome_profile,
        launch_flow_chrome_profile,
    )

    profile = get_playwright_flow_chrome_profile()
    port = get_playwright_flow_cdp_port()
    ensure_flow_launch_script()
    print(f"  Kiem tra profile CDP {profile}...", flush=True)
    r = launch_flow_chrome_profile(kill_chrome_first=False, wait_for_cdp=False)
    print(r.get("message") or r, flush=True)
    if not r.get("ok"):
        return 1
    import time

    time.sleep(5)
    print(f"  {diagnose_chrome_cdp(port)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
