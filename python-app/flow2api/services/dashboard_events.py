from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


class DashboardEvents:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue[str]] = []

    def publish(self, event: str, data: dict | None = None) -> None:
        payload = json.dumps(data or {}, ensure_ascii=False)
        line = f"event: {event}\ndata: {payload}\n\n"
        dead: list[asyncio.Queue[str]] = []
        for q in self._subs:
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            if q in self._subs:
                self._subs.remove(q)

    async def stream(self) -> AsyncIterator[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subs.append(q)
        try:
            yield ": connected\n\n"
            while True:
                msg = await q.get()
                yield msg
        finally:
            if q in self._subs:
                self._subs.remove(q)


events = DashboardEvents()
