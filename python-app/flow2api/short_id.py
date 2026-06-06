"""Numeric public request IDs (compatible with dashboard)."""
from __future__ import annotations

import random
import time


def new_request_id() -> str:
    base = int(time.time())
    return str(base * 1000 + random.randint(0, 999))
