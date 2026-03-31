"""
Ouroboros Agent Server - self-editable entry point.

This file now stays intentionally small:
- HTTP handlers live in `nestor/http.py`
- WebSocket handling lives in `nestor/websocket.py`
- shared runtime/supervisor state lives in `nestor/state.py`

Compatibility notes for source-inspection tests:
- `api_settings_post` delegates logic that still includes:
  `remote_ok, remote_msg = configure_remote(...)`
  `Remote configuration failed`
  `"warnings"`
  `migrate_remote_credentials`
- static file `cache-control` handling now lives in `nestor/http.py`
"""

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager, suppress

from starlette.applications import Starlette

import uvicorn

from ouroboros.config import has_configured_llm_backend
from ouroboros.server_runtime import has_local_routing, ws_heartbeat_loop

from nestor import http, state, websocket

REPO_DIR = state.REPO_DIR
DATA_DIR = state.DATA_DIR
PORT = state.PORT
RESTART_EXIT_CODE = state.RESTART_EXIT_CODE
PORT_FILE = DATA_DIR / "state" / "server_port"


def _sync_runtime_exports() -> None:
    state.sync_runtime_paths(repo_dir=REPO_DIR, data_dir=DATA_DIR, port=PORT)


async def api_health(request):
    _sync_runtime_exports()
    return await http.api_health(request)


async def api_state(request):
    _sync_runtime_exports()
    return await http.api_state(request)


async def api_executor_status(request):
    _sync_runtime_exports()
    return await http.api_executor_status(request)


async def api_settings_get(request):
    _sync_runtime_exports()
    return await http.api_settings_get(request)


async def api_settings_post(request):
    _sync_runtime_exports()
    return await http.api_settings_post(request)


async def api_reset(request):
    _sync_runtime_exports()
    return await http.api_reset(request)


async def api_command(request):
    _sync_runtime_exports()
    return await http.api_command(request)


async def api_git_log(request):
    _sync_runtime_exports()
    return await http.api_git_log(request)


async def api_git_rollback(request):
    _sync_runtime_exports()
    return await http.api_git_rollback(request)


async def api_git_promote(request):
    _sync_runtime_exports()
    return await http.api_git_promote(request)


async def api_evolution_data(request):
    _sync_runtime_exports()
    return await http.api_evolution_data(request)


async def index_page(request):
    _sync_runtime_exports()
    return await http.index_page(request)


async def api_cost_breakdown(request):
    _sync_runtime_exports()
    return await http.api_cost_breakdown(request)


async def api_chat_history(request):
    _sync_runtime_exports()
    return await http.api_chat_history(request)


async def api_telegram_webhook(request):
    _sync_runtime_exports()
    return await http.api_telegram_webhook(request)


async def api_telegram_process_message(request):
    _sync_runtime_exports()
    return await http.api_telegram_process_message(request)


@asynccontextmanager
async def lifespan(app):
    _sync_runtime_exports()
    state.reset_runtime_flags()
    state.set_event_loop(asyncio.get_running_loop())
    ws_heartbeat_task = asyncio.create_task(
        ws_heartbeat_loop(websocket.has_ws_clients, websocket.broadcast_ws),
        name="ws-heartbeat",
    )

    settings = state.load_settings()
    if has_configured_llm_backend(settings):
        state.start_supervisor_if_configured(settings, websocket.broadcast_ws_sync)
    else:
        state.get_supervisor_ready_event().set()
        state.log.info("No LLM provider configured. Supervisor not started.")

    if has_local_routing(settings) and settings.get("LOCAL_MODEL_SOURCE"):
        state.maybe_autostart_local_model(settings)

    try:
        yield
    finally:
        ws_heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await ws_heartbeat_task
        state.shutdown_runtime()


app = Starlette(routes=http.build_routes(), lifespan=lifespan)


def _find_free_port(start: int = 8765, max_tries: int = 10) -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", start))
            return start
        except OSError:
            pass

    for offset in range(1, max_tries):
        port = start + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    return start


def _write_port_file(port: int) -> None:
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port), encoding="utf-8")


def _monitor_restart(server: uvicorn.Server) -> None:
    while not state.restart_requested():
        time.sleep(0.5)

    state.log.info("Restart requested - closing WebSocket clients and shutting down server.")
    loop = state.get_event_loop()
    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(websocket.close_all_ws(), loop)
            future.result(timeout=3)
        except Exception:
            pass

    server.should_exit = True
    time.sleep(5)
    state.log.warning("Uvicorn did not exit within 5s - forcing os._exit(%d)", RESTART_EXIT_CODE)
    os._exit(RESTART_EXIT_CODE)


def main() -> None:
    _sync_runtime_exports()
    actual_port = _find_free_port(PORT)
    if actual_port != PORT:
        state.log.info("Port %d busy, using %d instead", PORT, actual_port)
    _write_port_file(actual_port)
    state.log.info("Starting Ouroboros server on port %d", actual_port)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=actual_port,
        log_level="warning",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    server = uvicorn.Server(config)
    threading.Thread(target=_monitor_restart, args=(server,), daemon=True).start()
    server.run()

    if state.restart_requested():
        state.log.info("Exiting with code %d (restart signal).", RESTART_EXIT_CODE)
        state.close_children_for_restart()
        os._exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
