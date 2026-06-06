from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.server import WebSocketServerProtocol

from flow2api.config import WS_HOST, WS_PORT
from flow2api.services.extension_pool import get_extension_pool

logger = logging.getLogger(__name__)


async def _handler(ws: WebSocketServerProtocol) -> None:
    pool = get_extension_pool()
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await pool.handle_ws_message(ws, data)
    except websockets.ConnectionClosed:
        pass
    finally:
        await pool.unregister_ws(ws)


async def run_ws_server() -> None:
    async with websockets.serve(_handler, WS_HOST, WS_PORT):
        logger.info("WebSocket server ws://%s:%s (multi-profile pool)", WS_HOST, WS_PORT)
        await asyncio.Future()
