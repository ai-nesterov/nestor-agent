"""WebSocket client management and endpoint handling."""

import asyncio
import json
import threading
from datetime import datetime, timezone
from typing import List

from starlette.websockets import WebSocket, WebSocketDisconnect

from nestor import state

_ws_clients: List[WebSocket] = []
_ws_lock = threading.Lock()


def has_ws_clients() -> bool:
    with _ws_lock:
        return bool(_ws_clients)


async def broadcast_ws(msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False, default=str)
    with _ws_lock:
        clients = list(_ws_clients)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            state.log.debug("Dropping dead WebSocket client during broadcast", exc_info=True)
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                try:
                    _ws_clients.remove(ws)
                except ValueError:
                    pass


def broadcast_ws_sync(msg: dict) -> None:
    loop = state.get_event_loop()
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast_ws(msg), loop)
    except RuntimeError:
        pass


async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    with _ws_lock:
        _ws_clients.append(websocket)
        total = len(_ws_clients)
    state.log.info("WebSocket client connected (total: %d)", total)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            payload = msg.get("content", "") if msg_type == "chat" else msg.get("cmd", "")
            if msg_type in ("chat", "command") and payload:
                try:
                    from supervisor.message_bus import get_bridge

                    get_bridge().ui_send(payload)
                except Exception:
                    ts = datetime.now(timezone.utc).isoformat()
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "chat",
                                "role": "assistant",
                                "content": "⚠️ System is still initializing. Please wait a moment and try again.",
                                "ts": ts,
                            }
                        )
                    )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        state.log.warning("WebSocket error: %s", exc)
    finally:
        with _ws_lock:
            try:
                _ws_clients.remove(websocket)
            except ValueError:
                pass
            total = len(_ws_clients)
        state.log.info("WebSocket client disconnected (total: %d)", total)


async def close_all_ws(*, code: int = 1012, reason: str = "Server restarting") -> None:
    with _ws_lock:
        clients = list(_ws_clients)
    for ws in clients:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass


__all__ = [
    "broadcast_ws",
    "broadcast_ws_sync",
    "close_all_ws",
    "has_ws_clients",
    "ws_endpoint",
]
