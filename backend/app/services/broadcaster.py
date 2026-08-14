import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class Broadcaster:
    """Simple in-process WebSocket fan-out.

    All connected clients are kept in ``self._clients``; ``publish`` pushes
    one JSON message to each.  Network failures drop the offending client.
    """

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self, ws: Any) -> None:
        async with self._lock:
            self._clients.add(ws)
        log.info({"event": "ws.subscribe", "clients": len(self._clients)})

    async def unsubscribe(self, ws: Any) -> None:
        async with self._lock:
            self._clients.discard(ws)
        log.info({"event": "ws.unsubscribe", "clients": len(self._clients)})

    async def publish(self, message: dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(message, default=str)
        dead: list[Any] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception as exc:  # noqa: BLE001
                log.warning({"event": "ws.send_failed", "error": str(exc)})
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


broadcaster = Broadcaster()
