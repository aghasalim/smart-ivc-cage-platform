from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from .security import decode_token
from .services.broadcaster import broadcaster

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(
    ws: WebSocket,
    token: str = Query(default=""),
) -> None:
    try:
        decode_token(token)
    except Exception:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    await broadcaster.subscribe(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.unsubscribe(ws)
