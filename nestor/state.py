"""Shared server runtime state and supervisor lifecycle."""

import asyncio
import logging
import os
import pathlib
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, Optional

from ouroboros.config import (
    SETTINGS_DEFAULTS,
    apply_settings_to_env as _apply_settings_to_env,
    has_configured_llm_backend,
    load_settings,
    save_settings,
)
from ouroboros.server_runtime import has_local_routing, setup_remote_if_configured

REPO_DIR = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", pathlib.Path(__file__).resolve().parent.parent))
DATA_DIR = pathlib.Path(
    os.environ.get("OUROBOROS_DATA_DIR", pathlib.Path.home() / "Ouroboros" / "data")
)
PORT = int(os.environ.get("OUROBOROS_SERVER_PORT", "8765"))

sys.path.insert(0, str(REPO_DIR))

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_dir = DATA_DIR / "logs"
_handlers = [logging.StreamHandler()]
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _log_dir / "server.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _handlers.insert(0, _file_handler)
except OSError:
    _file_handler = None
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=_handlers,
)
log = logging.getLogger("server")

RESTART_EXIT_CODE = 42
PANIC_EXIT_CODE = 99
APP_START = time.time()

_restart_requested = threading.Event()
_supervisor_ready = threading.Event()
_supervisor_error: Optional[str] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_evo_cache: Dict[str, Any] = {}


def sync_runtime_paths(*, repo_dir: pathlib.Path, data_dir: pathlib.Path, port: int) -> None:
    """Keep path/config globals mutable for compatibility with `server` re-exports."""
    global REPO_DIR, DATA_DIR, PORT

    REPO_DIR = pathlib.Path(repo_dir)
    DATA_DIR = pathlib.Path(data_dir)
    PORT = int(port)


def set_event_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    global _event_loop
    _event_loop = loop


def get_event_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _event_loop


def get_supervisor_ready_event() -> threading.Event:
    return _supervisor_ready


def supervisor_is_ready() -> bool:
    return _supervisor_ready.is_set()


def get_supervisor_error() -> Optional[str]:
    return _supervisor_error


def get_evolution_cache() -> Dict[str, Any]:
    return _evo_cache


def request_restart_exit() -> None:
    _restart_requested.set()


def restart_requested() -> bool:
    return _restart_requested.is_set()


def reset_runtime_flags() -> None:
    global _supervisor_error
    _restart_requested.clear()
    _supervisor_ready.clear()
    _supervisor_error = None


def _handle_restart_in_supervisor(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(
            int(st["owner_chat_id"]),
            f"♻️ Restart requested by agent: {evt.get('reason')}",
        )
    ok, msg = ctx.safe_restart(
        reason="agent_restart_request",
        unsynced_policy="rescue_and_reset",
    )
    if not ok:
        if st.get("owner_chat_id"):
            ctx.send_with_budget(int(st["owner_chat_id"]), f"⚠️ Restart skipped: {msg}")
        return
    ctx.kill_workers()
    st2 = ctx.load_state()
    st2["session_id"] = uuid.uuid4().hex
    ctx.save_state(st2)
    ctx.persist_queue_snapshot(reason="pre_restart_exit")
    request_restart_exit()


def execute_panic_stop(consciousness, kill_workers_fn) -> None:
    """Full emergency stop: kill everything, write panic flag, hard-exit."""
    log.critical("PANIC STOP initiated.")
    try:
        consciousness.stop()
    except Exception:
        pass

    try:
        from supervisor.state import load_state, save_state

        st = load_state()
        st["evolution_mode_enabled"] = False
        st["bg_consciousness_enabled"] = False
        save_state(st)
    except Exception:
        pass

    try:
        panic_flag = DATA_DIR / "state" / "panic_stop.flag"
        panic_flag.parent.mkdir(parents=True, exist_ok=True)
        panic_flag.write_text("panic", encoding="utf-8")
    except Exception:
        pass

    try:
        from ouroboros.local_model import get_manager

        get_manager().stop_server()
    except Exception:
        pass

    try:
        from ouroboros.tools.shell import kill_all_tracked_subprocesses

        kill_all_tracked_subprocesses()
    except Exception:
        pass

    try:
        kill_workers_fn(force=True)
    except Exception:
        pass

    log.critical("PANIC STOP complete - hard exit with code %d.", PANIC_EXIT_CODE)
    os._exit(PANIC_EXIT_CODE)


def run_supervisor(settings: dict, broadcast_ws_sync: Callable[[dict], None]) -> None:
    """Initialize and run the supervisor loop in a background thread."""
    global _supervisor_error

    _apply_settings_to_env(settings)

    try:
        from supervisor.message_bus import LocalChatBridge, init as bus_init

        bridge = LocalChatBridge()
        bridge._broadcast_fn = broadcast_ws_sync

        from ouroboros.utils import set_log_sink

        set_log_sink(bridge.push_log)

        bus_init(
            drive_root=DATA_DIR,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            budget_report_every=10,
            chat_bridge=bridge,
        )

        from supervisor.state import (
            append_jsonl,
            init as state_init,
            init_state,
            load_state,
            rotate_chat_log_if_needed,
            save_state,
            update_budget_from_usage,
        )

        state_init(DATA_DIR, float(settings.get("TOTAL_BUDGET", 10.0)))
        init_state()

        from supervisor.git_ops import ensure_repo_present, init as git_ops_init, safe_restart

        git_ops_init(
            repo_dir=REPO_DIR,
            drive_root=DATA_DIR,
            remote_url="",
            branch_dev="ouroboros",
            branch_stable="ouroboros-stable",
        )
        ensure_repo_present()
        setup_remote_if_configured(settings, log)
        ok, msg = safe_restart(reason="bootstrap", unsynced_policy="rescue_and_reset")
        if not ok:
            log.error("Supervisor bootstrap failed: %s", msg)

        from supervisor.queue import (
            cancel_task_by_id,
            enqueue_evolution_task_if_needed,
            enforce_task_timeouts,
            persist_queue_snapshot,
            queue_review_task,
            reconcile_orphaned_scheduled_results,
            restore_pending_from_snapshot,
            sort_pending,
        )
        from supervisor.workers import (
            PENDING,
            RUNNING,
            WORKERS,
            _get_chat_agent,
            assign_tasks,
            auto_resume_after_restart,
            ensure_workers_healthy,
            get_event_q,
            handle_chat_direct,
            init as workers_init,
            kill_workers,
            spawn_workers,
        )

        max_workers = int(settings.get("OUROBOROS_MAX_WORKERS", 5))
        soft_timeout = int(settings.get("OUROBOROS_SOFT_TIMEOUT_SEC", 600))
        hard_timeout = int(settings.get("OUROBOROS_HARD_TIMEOUT_SEC", 1800))

        workers_init(
            repo_dir=REPO_DIR,
            drive_root=DATA_DIR,
            max_workers=max_workers,
            soft_timeout=soft_timeout,
            hard_timeout=hard_timeout,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            branch_dev="ouroboros",
            branch_stable="ouroboros-stable",
        )

        from supervisor.events import dispatch_event
        from supervisor.message_bus import send_with_budget
        from ouroboros.consciousness import BackgroundConsciousness
        import queue as _queue_mod
        import types

        kill_workers()
        spawn_workers(max_workers)
        restored_pending = restore_pending_from_snapshot()
        persist_queue_snapshot(reason="startup")

        if restored_pending > 0:
            st_boot = load_state()
            if st_boot.get("owner_chat_id"):
                send_with_budget(
                    int(st_boot["owner_chat_id"]),
                    f"♻️ Restored pending queue from snapshot: {restored_pending} tasks.",
                )

        auto_resume_after_restart()

        def _get_owner_chat_id() -> Optional[int]:
            try:
                st = load_state()
                cid = st.get("owner_chat_id")
                return int(cid) if cid else None
            except Exception:
                return None

        consciousness = BackgroundConsciousness(
            drive_root=DATA_DIR,
            repo_dir=REPO_DIR,
            event_queue=get_event_q(),
            owner_chat_id_fn=_get_owner_chat_id,
        )

        bg_state = load_state()
        if bg_state.get("bg_consciousness_enabled"):
            consciousness.start()
            log.info("Background consciousness auto-restored from saved state.")

        event_ctx = types.SimpleNamespace(
            DRIVE_ROOT=DATA_DIR,
            REPO_DIR=REPO_DIR,
            BRANCH_DEV="ouroboros",
            BRANCH_STABLE="ouroboros-stable",
            bridge=bridge,
            WORKERS=WORKERS,
            PENDING=PENDING,
            RUNNING=RUNNING,
            MAX_WORKERS=max_workers,
            send_with_budget=send_with_budget,
            load_state=load_state,
            save_state=save_state,
            update_budget_from_usage=update_budget_from_usage,
            append_jsonl=append_jsonl,
            enqueue_task=None,
            cancel_task_by_id=cancel_task_by_id,
            queue_review_task=queue_review_task,
            persist_queue_snapshot=persist_queue_snapshot,
            safe_restart=safe_restart,
            kill_workers=kill_workers,
            spawn_workers=spawn_workers,
            sort_pending=sort_pending,
            consciousness=consciousness,
            request_restart=request_restart_exit,
        )
    except Exception as exc:
        _supervisor_error = f"Supervisor init failed: {exc}"
        log.critical("Supervisor initialization failed", exc_info=True)
        _supervisor_ready.set()
        return

    _supervisor_ready.set()
    log.info("Supervisor ready.")

    offset = 0
    crash_count = 0
    while not restart_requested():
        try:
            rotate_chat_log_if_needed(DATA_DIR)
            ensure_workers_healthy()

            event_q = get_event_q()
            while True:
                try:
                    evt = event_q.get_nowait()
                except _queue_mod.Empty:
                    break
                if evt.get("type") == "restart_request":
                    _handle_restart_in_supervisor(evt, event_ctx)
                    continue
                dispatch_event(evt, event_ctx)

            enforce_task_timeouts()
            enqueue_evolution_task_if_needed()
            assign_tasks()
            reconcile_orphaned_scheduled_results()
            persist_queue_snapshot(reason="main_loop")

            updates = bridge.get_updates(offset=offset, timeout=1)
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                msg = upd.get("message") or {}
                if not msg:
                    continue

                if isinstance(msg, dict) and msg.get("type") == "telegram_message":
                    chat_id = msg.get("chat_id")
                    text = msg.get("text", "")
                    now_iso = msg.get("ts", datetime.now(timezone.utc).isoformat())
                    if not chat_id or not text:
                        continue

                    user_id = 1
                    st = load_state()
                    if st.get("owner_id") is None:
                        st["owner_id"] = user_id
                        st["owner_chat_id"] = chat_id

                    from supervisor.message_bus import log_chat

                    log_chat("in", chat_id, user_id, text)
                    st["last_owner_message_at"] = now_iso
                    save_state(st)

                    consciousness.inject_observation(f"Telegram message from {chat_id}: {text}")
                    agent = _get_chat_agent()
                    if agent._busy:
                        agent.inject_message(text)
                    else:
                        consciousness.pause()

                        def _run_telegram_and_resume(cid, txt, reply_cid):
                            try:
                                handle_chat_direct(cid, txt, None, telegram_chat_id=reply_cid)
                            finally:
                                consciousness.resume()

                        threading.Thread(
                            target=_run_telegram_and_resume,
                            args=(chat_id, text, chat_id),
                            daemon=True,
                        ).start()
                    continue

                chat_id = 1
                user_id = 1
                text = str(msg.get("text") or "")
                now_iso = datetime.now(timezone.utc).isoformat()

                st = load_state()
                if st.get("owner_id") is None:
                    st["owner_id"] = user_id
                    st["owner_chat_id"] = chat_id

                from supervisor.message_bus import log_chat

                log_chat("in", chat_id, user_id, text)
                st["last_owner_message_at"] = now_iso
                save_state(st)

                if not text:
                    continue

                lowered = text.strip().lower()
                if lowered.startswith("/panic"):
                    send_with_budget(chat_id, "🛑 PANIC: killing everything. App will close.")
                    execute_panic_stop(consciousness, kill_workers)
                elif lowered.startswith("/restart"):
                    send_with_budget(chat_id, "♻️ Restarting (soft).")
                    ok, restart_msg = safe_restart(
                        reason="owner_restart",
                        unsynced_policy="rescue_and_reset",
                    )
                    if not ok:
                        send_with_budget(chat_id, f"⚠️ Restart cancelled: {restart_msg}")
                        continue
                    kill_workers()
                    request_restart_exit()
                elif lowered.startswith("/review"):
                    queue_review_task(reason="owner:/review", force=True)
                elif lowered.startswith("/evolve"):
                    parts = lowered.split(maxsplit=2)
                    action = parts[1] if len(parts) > 1 else "on"
                    turn_on = action not in ("off", "stop", "0")
                    st2 = load_state()
                    st2["evolution_mode_enabled"] = bool(turn_on)
                    if turn_on:
                        st2["evolution_consecutive_failures"] = 0
                        st2["evolution_waiting_for_owner"] = False
                        st2["evolution_blocked_reason"] = ""
                    save_state(st2)
                    if not turn_on:
                        PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
                        sort_pending()
                        persist_queue_snapshot(reason="evolve_off")
                    send_with_budget(chat_id, f"🧬 Evolution: {'ON' if turn_on else 'OFF'}")
                elif lowered.startswith("/bg"):
                    parts = lowered.split()
                    action = parts[1] if len(parts) > 1 else "status"
                    if action in ("start", "on", "1"):
                        result = consciousness.start()
                        bg_state = load_state()
                        bg_state["bg_consciousness_enabled"] = True
                        save_state(bg_state)
                        send_with_budget(chat_id, f"🧠 {result}")
                    elif action in ("stop", "off", "0"):
                        result = consciousness.stop()
                        bg_state = load_state()
                        bg_state["bg_consciousness_enabled"] = False
                        save_state(bg_state)
                        send_with_budget(chat_id, f"🧠 {result}")
                    else:
                        bg_status = "running" if consciousness.is_running else "stopped"
                        send_with_budget(chat_id, f"🧠 Background consciousness: {bg_status}")
                elif lowered.startswith("/status"):
                    from supervisor.state import status_text

                    status = status_text(WORKERS, PENDING, RUNNING, soft_timeout, hard_timeout)
                    send_with_budget(chat_id, status, force_budget=True)
                else:
                    consciousness.inject_observation(f"Owner message: {text}")
                    agent = _get_chat_agent()
                    if agent._busy:
                        agent.inject_message(text)
                    else:
                        consciousness.pause()

                        def _run_and_resume(cid, txt):
                            try:
                                handle_chat_direct(cid, txt, None)
                            finally:
                                consciousness.resume()

                        threading.Thread(
                            target=_run_and_resume,
                            args=(chat_id, text),
                            daemon=True,
                        ).start()

            crash_count = 0
            time.sleep(0.5)
        except Exception as exc:
            crash_count += 1
            log.error("Supervisor loop crash #%d: %s", crash_count, exc, exc_info=True)
            if crash_count >= 3:
                log.critical("Supervisor exceeded max retries.")
                return
            time.sleep(min(30, 2**crash_count))


def start_supervisor_if_configured(
    settings: dict,
    broadcast_ws_sync: Callable[[dict], None],
) -> None:
    if has_configured_llm_backend(settings):
        threading.Thread(
            target=run_supervisor,
            args=(settings, broadcast_ws_sync),
            daemon=True,
        ).start()
    else:
        _supervisor_ready.set()
        log.info("No LLM provider configured. Supervisor not started.")


def maybe_autostart_local_model(settings: dict) -> None:
    if has_local_routing(settings) and settings.get("LOCAL_MODEL_SOURCE"):
        from ouroboros.local_model_autostart import auto_start_local_model

        threading.Thread(
            target=auto_start_local_model,
            args=(settings,),
            daemon=True,
            name="local-model-autostart",
        ).start()


def shutdown_runtime() -> None:
    log.info("Server shutting down...")
    try:
        from ouroboros.local_model import get_manager

        get_manager().stop_server()
    except Exception:
        pass
    try:
        from ouroboros.tools.shell import kill_all_tracked_subprocesses

        kill_all_tracked_subprocesses()
    except Exception:
        pass
    try:
        from supervisor.workers import kill_workers

        kill_workers(force=True)
    except Exception:
        pass


def close_children_for_restart() -> None:
    try:
        from ouroboros.tools.shell import kill_all_tracked_subprocesses

        kill_all_tracked_subprocesses()
    except Exception:
        pass
    try:
        from supervisor.workers import kill_workers

        kill_workers(force=True)
    except Exception:
        pass
    import multiprocessing

    from ouroboros.compat import force_kill_pid

    for child in multiprocessing.active_children():
        try:
            force_kill_pid(child.pid)
        except (ProcessLookupError, PermissionError):
            pass


__all__ = [
    "APP_START",
    "DATA_DIR",
    "PORT",
    "REPO_DIR",
    "RESTART_EXIT_CODE",
    "SETTINGS_DEFAULTS",
    "close_children_for_restart",
    "execute_panic_stop",
    "get_event_loop",
    "get_evolution_cache",
    "get_supervisor_error",
    "get_supervisor_ready_event",
    "load_settings",
    "log",
    "maybe_autostart_local_model",
    "request_restart_exit",
    "reset_runtime_flags",
    "restart_requested",
    "save_settings",
    "set_event_loop",
    "shutdown_runtime",
    "start_supervisor_if_configured",
    "supervisor_is_ready",
    "sync_runtime_paths",
]
