"""
Supervisor — Worker lifecycle management.

Multiprocessing workers, worker health, direct chat handling.
Queue operations moved to supervisor.queue.
"""

from __future__ import annotations
import logging
log = logging.getLogger(__name__)

import datetime
import json
import multiprocessing as mp
import os
import pathlib
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from supervisor.state import load_state, append_jsonl
from supervisor import git_ops
from supervisor.message_bus import send_with_budget
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    load_task_result,
    write_task_result,
)
from ouroboros.utils import utc_now_iso


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
REPO_DIR: pathlib.Path = pathlib.Path.home() / "Ouroboros" / "repo"
DRIVE_ROOT: pathlib.Path = pathlib.Path.home() / "Ouroboros" / "data"
MAX_WORKERS: int = 5
SOFT_TIMEOUT_SEC: int = 600
HARD_TIMEOUT_SEC: int = 1800
HEARTBEAT_STALE_SEC: int = 120
QUEUE_MAX_RETRIES: int = 1
TOTAL_BUDGET_LIMIT: float = 0.0
BRANCH_DEV: str = "ouroboros"
BRANCH_STABLE: str = "ouroboros-stable"

_CTX = None
_LAST_SPAWN_TIME: float = 0.0  # grace period: don't count dead workers right after spawn
_SPAWN_GRACE_SEC: float = 90.0  # workers need up to ~60s to init (spawn + pip)

# "spawn" re-imports __main__ in child processes, which in PyInstaller frozen apps
# causes fork bombs (each child re-runs the full app). Use "fork" by default on
# Linux and macOS. Workers don't touch GUI, so fork is safe.
# Windows only supports "spawn".
_DEFAULT_WORKER_START_METHOD = "spawn" if sys.platform == "win32" else "fork"
_WORKER_START_METHOD = str(os.environ.get("OUROBOROS_WORKER_START_METHOD", _DEFAULT_WORKER_START_METHOD) or _DEFAULT_WORKER_START_METHOD).strip().lower()
if _WORKER_START_METHOD not in {"fork", "spawn", "forkserver"}:
    _WORKER_START_METHOD = _DEFAULT_WORKER_START_METHOD


def _get_ctx():
    """Return multiprocessing context used for worker processes."""
    global _CTX
    if _CTX is None:
        _CTX = mp.get_context(_WORKER_START_METHOD)
    return _CTX


def init(repo_dir: pathlib.Path, drive_root: pathlib.Path, max_workers: int,
         soft_timeout: int, hard_timeout: int, total_budget_limit: float,
         branch_dev: str = "ouroboros", branch_stable: str = "ouroboros-stable") -> None:
    global REPO_DIR, DRIVE_ROOT, MAX_WORKERS, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC
    global TOTAL_BUDGET_LIMIT, BRANCH_DEV, BRANCH_STABLE
    REPO_DIR = repo_dir
    DRIVE_ROOT = drive_root
    MAX_WORKERS = max_workers
    SOFT_TIMEOUT_SEC = soft_timeout
    HARD_TIMEOUT_SEC = hard_timeout
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BRANCH_DEV = branch_dev
    BRANCH_STABLE = branch_stable

    # Initialize queue module
    from supervisor import queue
    queue.init(drive_root, soft_timeout, hard_timeout)
    queue.init_queue_refs(PENDING, RUNNING, QUEUE_SEQ_COUNTER_REF)


# ---------------------------------------------------------------------------
# Worker data structures
# ---------------------------------------------------------------------------

@dataclass
class Worker:
    wid: int
    proc: mp.Process
    in_q: Any
    busy_task_id: Optional[str] = None
    kind: str = "ouroboros"


_EVENT_Q = None


def get_event_q():
    """Get the current EVENT_Q, creating if needed."""
    global _EVENT_Q
    if _EVENT_Q is None:
        _EVENT_Q = _get_ctx().Queue()
    return _EVENT_Q


WORKERS: Dict[int, Worker] = {}
PENDING: List[Dict[str, Any]] = []
RUNNING: Dict[str, Dict[str, Any]] = {}
CRASH_TS: List[float] = []
QUEUE_SEQ_COUNTER_REF: Dict[str, int] = {"value": 0}

# Lock for all mutations to PENDING, RUNNING, WORKERS shared collections.
# Canonical definition lives in queue.py; imported here for use by assign_tasks/kill_workers.
from supervisor.queue import _queue_lock


def get_running_task_ids() -> List[str]:
    """Return list of task IDs currently being processed by workers."""
    return [w.busy_task_id for w in WORKERS.values() if w.busy_task_id]


# ---------------------------------------------------------------------------
# Chat agent (direct mode)
# ---------------------------------------------------------------------------
_chat_agent = None


def _get_chat_agent():
    global _chat_agent
    if _chat_agent is None:
        if not getattr(sys, 'frozen', False):
            sys.path.insert(0, str(REPO_DIR))
        from ouroboros.agent import make_agent
        _chat_agent = make_agent(
            repo_dir=str(REPO_DIR),
            drive_root=str(DRIVE_ROOT),
            event_queue=get_event_q(),
        )
    return _chat_agent


def handle_chat_direct(chat_id: int, text: str, image_data: Optional[Union[Tuple[str, str], Tuple[str, str, str]]] = None, telegram_chat_id: Optional[int] = None) -> None:
    from supervisor.state import budget_remaining, load_state
    if budget_remaining(load_state()) <= 0:
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().send_message(chat_id, "🚫 Budget exhausted. Task rejected. Please increase TOTAL_BUDGET in settings.")
        except Exception:
            pass
        return
        
    try:
        agent = _get_chat_agent()
        task = {
            "id": uuid.uuid4().hex[:8],
            "type": "task",
            "chat_id": chat_id,
            "text": text,
            "_is_direct_chat": True,
        }
        if telegram_chat_id:
            task["_telegram_chat_id"] = telegram_chat_id
        if image_data:
            # image_data is (base64, mime) or (base64, mime, caption)
            task["image_base64"] = image_data[0]
            task["image_mime"] = image_data[1]
            if len(image_data) > 2 and image_data[2]:
                task["image_caption"] = image_data[2]
                # Prefer caption as task text if text is empty
                if not text:
                    task["text"] = image_data[2]
        # Fallback for truly empty messages
        if not task["text"]:
            task["text"] = "(image attached)" if image_data else ""
        events = agent.handle_task(task)
        for e in events:
            get_event_q().put(e)
    except Exception as e:
        import traceback
        err_msg = f"⚠️ Error: {type(e).__name__}: {e}"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "direct_chat_error",
                "error": repr(e),
                "traceback": str(traceback.format_exc())[:2000],
            },
        )
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().send_message(chat_id, err_msg)
        except Exception:
            log.debug("Suppressed exception", exc_info=True)


# ---------------------------------------------------------------------------
# Auto-resume after restart
# ---------------------------------------------------------------------------

def auto_resume_after_restart() -> None:
    """If recent restart left open work, auto-resume without waiting for owner message.

    Checks: scratchpad content, recent restart events, pending_restart_verify.
    Background consciousness will subsume this eventually, but auto-resume is
    needed immediately after a restart so the agent doesn't go silent.
    """
    try:
        # Panic flag: skip auto-resume after emergency stop (consumed on check)
        panic_flag = DRIVE_ROOT / "state" / "panic_stop.flag"
        if panic_flag.exists():
            panic_flag.unlink(missing_ok=True)
            log.info("Panic flag detected — skipping auto-resume.")
            return

        st = load_state()
        chat_id = st.get("owner_chat_id")
        if not chat_id:
            return

        # Check for recent restart (within 2 minutes)
        restart_verify_path = DRIVE_ROOT / "state" / "pending_restart_verify.json"
        recent_restart = False
        if restart_verify_path.exists():
            recent_restart = True
        else:
            # Check supervisor.jsonl for recent restart event
            sup_log = DRIVE_ROOT / "logs" / "supervisor.jsonl"
            if sup_log.exists():
                try:
                    lines = sup_log.read_text(encoding="utf-8").strip().split("\n")
                    for line in reversed(lines[-20:]):
                        if not line.strip():
                            continue
                        evt = json.loads(line)
                        if evt.get("type") in ("launcher_start", "restart"):
                            recent_restart = True
                            break
                except Exception:
                    log.debug("Suppressed exception", exc_info=True)

        if not recent_restart:
            return

        # Check if scratchpad has meaningful content
        scratchpad_path = DRIVE_ROOT / "memory" / "scratchpad.md"
        if not scratchpad_path.exists():
            return

        scratchpad = scratchpad_path.read_text(encoding="utf-8")
        # Skip if scratchpad is empty or default
        stripped = scratchpad.strip()
        if not stripped or stripped == "# Scratchpad" or "(empty" in stripped.lower():
            # Check if it's just the default template with all empty sections
            content_lines = [
                ln.strip() for ln in stripped.splitlines()
                if ln.strip() and not ln.strip().startswith("#") and ln.strip() != "- (empty)"
            ]
            # Filter out UpdatedAt lines
            content_lines = [ln for ln in content_lines if not ln.startswith("UpdatedAt:")]
            if not content_lines:
                return

        # Auto-resume: inject synthetic message
        time.sleep(2)  # Let everything initialize
        agent = _get_chat_agent()
        if not agent._busy:
            import threading
            threading.Thread(
                target=handle_chat_direct,
                args=(int(chat_id),
                      "[auto-resume after restart] Continue your work. Read scratchpad and identity — they contain context of what you were doing.",
                      None),
                daemon=True,
            ).start()
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "auto_resume_triggered",
                },
            )
    except Exception as e:
        append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "auto_resume_error",
            "error": repr(e),
        })


def _record_external_task_running(
    *,
    drive_root: Union[pathlib.Path, str],
    task: Dict[str, Any],
    executor: str,
    started_iso: str,
    out_q: Any,
) -> None:
    task_id = str(task.get("id") or "")
    if not task_id:
        return
    root = pathlib.Path(drive_root)
    previous_status = "scheduled"
    try:
        existing = load_task_result(root, task_id)
        if isinstance(existing, dict):
            prev = str(existing.get("status") or "").strip().lower()
            if prev:
                previous_status = prev
    except Exception:
        pass

    try:
        write_task_result(
            root,
            task_id,
            STATUS_RUNNING,
            parent_task_id=task.get("parent_task_id"),
            description=task.get("description"),
            context=task.get("context"),
            executor=executor,
            repo_scope=task.get("repo_scope") or [],
            constraints=task.get("constraints") or {},
            artifact_policy=task.get("artifact_policy"),
            quota_class=task.get("quota_class"),
            priority=task.get("priority"),
            task_type=task.get("type"),
            task_kind=task.get("task_kind"),
            caller_class=task.get("caller_class"),
            model_policy=task.get("model_policy"),
            model_override=task.get("model_override"),
            importance=task.get("importance"),
            defer_on_quota=bool(task.get("defer_on_quota", True)),
            budget_decision=task.get("budget_decision"),
            result=f"External executor '{executor}' is running.",
            trace_summary=f"external_executor={executor}; status=running",
            cost_usd=0.0,
            total_rounds=0,
            ts=started_iso,
        )
    except Exception:
        log.warning("Failed to persist running task status for %s", task_id, exc_info=True)

    out_q.put(
        {
            "type": "log_event",
            "data": {
                "type": "status_transition",
                "ts": started_iso,
                "task_id": task_id,
                "executor": executor,
                "from_status": previous_status,
                "to_status": "running",
            },
        }
    )


# ---------------------------------------------------------------------------
# Worker process
# ---------------------------------------------------------------------------

def worker_main(wid: int, in_q: Any, out_q: Any, repo_dir: str, drive_root: str) -> None:
    import sys as _sys
    import traceback as _tb
    import pathlib as _pathlib
    if not getattr(_sys, 'frozen', False):
        _sys.path.insert(0, repo_dir)
    _drive = _pathlib.Path(drive_root)
    try:
        from ouroboros.agent import make_agent
        agent = make_agent(repo_dir=repo_dir, drive_root=drive_root, event_queue=out_q)
    except Exception as _e:
        _log_worker_crash(wid, _drive, "make_agent", _e, _tb.format_exc())
        return

    def _mark_external_task_running(task: Dict[str, Any], executor: str, started_iso: str) -> None:
        _record_external_task_running(
            drive_root=drive_root,
            task=task,
            executor=executor,
            started_iso=started_iso,
            out_q=out_q,
        )

    def _run_external_task(task: Dict[str, Any], executor: str) -> None:
        started_at = time.time()
        started_iso = utc_now_iso()
        task_id = str(task.get("id") or "")
        summary = ""
        result_text = ""
        status = STATUS_FAILED
        artifact_dir = ""
        changed_files: List[str] = []
        diff_stat: Dict[str, Any] = {"files": 0, "insertions": 0, "deletions": 0}
        tests_run: List[str] = []
        tests_passed: Optional[bool] = None
        cost_usd = 0.0
        total_rounds = 1

        _mark_external_task_running(task, executor, started_iso)
        out_q.put({
            "type": "log_event",
            "data": {
                "type": "executor_run",
                "ts": started_iso,
                "task_id": task_id,
                "executor": executor,
                "status": "running",
                "summary": "external executor task started",
            },
        })

        manager = None
        handle = None
        try:
            from ouroboros.executors.artifacts import ArtifactManager
            from ouroboros.executors.claude_code import ClaudeCodeRunner
            from ouroboros.executors.codex import CodexRunner
            from ouroboros.executors.worktree import WorktreeManager

            repo_path = _pathlib.Path(repo_dir)
            drive_path = _pathlib.Path(drive_root)
            worktrees_subdir = str(os.environ.get("EXECUTOR_WORKTREES_SUBDIR") or "worktrees")
            worktrees_root = drive_path / worktrees_subdir

            manager = WorktreeManager(repo_path, branch_dev=BRANCH_DEV, worktrees_root=worktrees_root)
            handle = manager.prepare_worktree(task_id=task_id, base_branch=BRANCH_DEV, executor=executor)

            artifacts = ArtifactManager(drive_path)
            artifact_path = artifacts.prepare_artifact_dir(task_id)
            artifact_dir = str(artifact_path)
            artifacts.write_text(
                artifact_path,
                "prompt.txt",
                str(task.get("description") or task.get("text") or ""),
            )

            if executor == "codex":
                runner = CodexRunner(
                    model=str(os.environ.get("CODEX_MODEL") or "gpt-5.4"),
                    auth_mode=str(os.environ.get("CODEX_AUTH_MODE") or "subscription_only"),
                    timeout_sec=int(os.environ.get("CODEX_EXECUTOR_TIMEOUT_SEC", os.environ.get("OUROBOROS_HARD_TIMEOUT_SEC", "1800"))),
                )
            else:
                runner = ClaudeCodeRunner(
                    model=str(os.environ.get("CLAUDE_CODE_MODEL") or "sonnet"),
                    auth_mode=str(os.environ.get("CLAUDE_CODE_AUTH_MODE") or "subscription_only"),
                    timeout_sec=int(os.environ.get("CLAUDE_EXECUTOR_TIMEOUT_SEC", os.environ.get("OUROBOROS_HARD_TIMEOUT_SEC", "1800"))),
                )

            ext_result = runner.run(task, worktree=handle, artifact_dir=artifact_path)
            manager.collect_patch(handle, artifact_path)

            usage = ext_result.usage if isinstance(ext_result.usage, dict) else {}
            try:
                cost_usd = float(usage.get("cost_usd") or 0.0)
            except Exception:
                cost_usd = 0.0

            payload: Dict[str, Any] = {}
            if ext_result.result_text:
                try:
                    candidate = json.loads(ext_result.result_text)
                    if isinstance(candidate, dict):
                        payload = candidate
                except Exception:
                    payload = {}
            if isinstance(payload.get("tests_run"), list):
                tests_run = [str(x) for x in payload.get("tests_run") or []]
            if isinstance(payload.get("tests_passed"), bool):
                tests_passed = bool(payload.get("tests_passed"))

            changed_files = list(ext_result.changed_files or [])
            diff_stat = dict(ext_result.diff_stat or {"files": 0, "insertions": 0, "deletions": 0})
            result_text = str(ext_result.result_text or "")
            summary = str(ext_result.summary or "")
            status = STATUS_COMPLETED if ext_result.status == "completed" else STATUS_FAILED

            artifacts.write_result(artifact_path, ext_result.to_dict())
            artifacts.write_manifest(
                artifact_path,
                {
                    "task_id": task_id,
                    "executor": executor,
                    "status": ext_result.status,
                    "summary": ext_result.summary,
                    "base_sha": ext_result.base_sha,
                    "worktree_path": ext_result.worktree_path,
                    "timings": ext_result.timings,
                    "auth_mode": usage.get("auth_mode"),
                    "model": usage.get("model"),
                },
            )
        except Exception as exc:
            summary = f"External executor crashed: {type(exc).__name__}: {exc}"
            result_text = summary
            status = STATUS_FAILED
        finally:
            retain = str(task.get("artifact_policy") or "patch_only").strip().lower() == "keep_worktree"
            try:
                if manager is not None and handle is not None:
                    manager.cleanup_worktree(handle, retain=retain)
            except Exception:
                pass

        finished_iso = utc_now_iso()
        duration_sec = round(max(0.0, time.time() - started_at), 3)
        write_task_result(
            drive_root,
            task_id,
            status,
            parent_task_id=task.get("parent_task_id"),
            description=task.get("description"),
            context=task.get("context"),
            executor=executor,
            repo_scope=task.get("repo_scope") or [],
            constraints=task.get("constraints") or {},
            artifact_policy=task.get("artifact_policy"),
            quota_class=task.get("quota_class"),
            priority=task.get("priority"),
            task_type=task.get("type"),
            task_kind=task.get("task_kind"),
            caller_class=task.get("caller_class"),
            model_policy=task.get("model_policy"),
            model_override=task.get("model_override"),
            importance=task.get("importance"),
            defer_on_quota=bool(task.get("defer_on_quota", True)),
            budget_decision=task.get("budget_decision"),
            result=result_text,
            trace_summary=f"external_executor={executor}; summary={summary}",
            cost_usd=round(float(cost_usd or 0.0), 6),
            total_rounds=total_rounds,
            artifact_dir=artifact_dir or None,
            changed_files=changed_files,
            diff_stat=diff_stat,
            tests_run=tests_run,
            tests_passed=tests_passed,
            timings={"started_at": started_iso, "finished_at": finished_iso, "duration_sec": duration_sec},
            ts=finished_iso,
        )
        out_q.put(
            {
                "type": "task_metrics",
                "worker_id": wid,
                "task_id": task_id,
                "task_type": task.get("type"),
                "duration_sec": duration_sec,
                "tool_calls": 0,
                "tool_errors": 0,
                "cost_usd": round(float(cost_usd or 0.0), 6),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_rounds": total_rounds,
                "ts": finished_iso,
            }
        )
        out_q.put(
            {
                "type": "task_done",
                "worker_id": wid,
                "task_id": task_id,
                "task_type": task.get("type"),
                "cost_usd": round(float(cost_usd or 0.0), 6),
                "total_rounds": total_rounds,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_sec": duration_sec,
                "ts": finished_iso,
            }
        )
        out_q.put(
            {
                "type": "log_event",
                "worker_id": wid,
                "data": {
                    "type": "executor_result",
                    "ts": finished_iso,
                    "task_id": task_id,
                    "executor": executor,
                    "status": "completed" if status == STATUS_COMPLETED else "failed",
                    "summary": summary,
                    "duration_sec": duration_sec,
                    "changed_files_count": len(changed_files),
                },
            }
        )

    while True:
        try:
            task = in_q.get()
            if task is None or task.get("type") == "shutdown":
                break
            executor = str(task.get("executor") or "ouroboros").strip().lower() or "ouroboros"
            if executor in {"codex", "claude_code"}:
                _run_external_task(task, executor)
            else:
                events = agent.handle_task(task)
                for e in events:
                    e2 = dict(e)
                    e2["worker_id"] = wid
                    out_q.put(e2)
        except Exception as _e:
            _log_worker_crash(wid, _drive, "handle_task", _e, _tb.format_exc())


def _write_failure_result(task_id: str, reason: str = "Worker process crashed (crash storm). Task was not completed.") -> None:
    """Write a failure result file for a crashed/orphaned task (zombie prevention)."""
    if not task_id:
        return
    try:
        from ouroboros.task_results import STATUS_FAILED, load_task_result, write_task_result
        existing = load_task_result(DRIVE_ROOT, task_id)
        if existing and existing.get("status") in ("completed", "failed"):
            return
        write_task_result(
            DRIVE_ROOT,
            task_id,
            STATUS_FAILED,
            result=reason,
            cost_usd=0,
            total_rounds=0,
        )
    except Exception:
        log.warning("Failed to write failure result for task %s", task_id, exc_info=True)


def _log_worker_crash(wid: int, drive_root: pathlib.Path, phase: str, exc: Exception, tb: str) -> None:
    """Best-effort: write crash info to supervisor.jsonl from inside worker process."""
    import os as _os
    try:
        path = drive_root / "logs" / "supervisor.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "worker_crash",
            "worker_id": wid,
            "pid": _os.getpid(),
            "phase": phase,
            "error": repr(exc),
            "traceback": str(tb)[:3000],
        }, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        log.debug("Suppressed exception", exc_info=True)


def _first_worker_boot_event_since(offset_bytes: int) -> Optional[Dict[str, Any]]:
    """Read first worker_boot event written after the given file offset."""
    path = DRIVE_ROOT / "logs" / "events.jsonl"
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            safe_offset = offset_bytes if 0 <= offset_bytes <= size else 0
            f.seek(safe_offset)
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        log.debug("Suppressed exception", exc_info=True)
        return None

    for line in data.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            log.debug("Suppressed exception in loop", exc_info=True)
            continue
        if isinstance(evt, dict) and str(evt.get("type") or "") == "worker_boot":
            return evt
    return None


def _verify_worker_sha_after_spawn(events_offset: int, timeout_sec: float = 90.0) -> None:
    """Verify that newly spawned workers booted with expected current_sha."""
    st = load_state()
    expected_sha = str(st.get("current_sha") or "").strip()
    if not expected_sha:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "worker_sha_verify_skipped",
                "reason": "missing_current_sha",
            },
        )
        return

    deadline = time.time() + max(float(timeout_sec), 1.0)
    boot_evt = None
    while time.time() < deadline:
        boot_evt = _first_worker_boot_event_since(events_offset)
        if boot_evt is not None:
            break
        time.sleep(0.25)

    if boot_evt is None:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "worker_sha_verify_timeout",
                "expected_sha": expected_sha,
            },
        )
        return

    observed_sha = str(boot_evt.get("git_sha") or "").strip()
    ok = bool(observed_sha and observed_sha == expected_sha)
    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "worker_sha_verify",
            "ok": ok,
            "expected_sha": expected_sha,
            "observed_sha": observed_sha,
            "worker_pid": boot_evt.get("pid"),
        },
    )
    if not ok and st.get("owner_chat_id"):
        send_with_budget(
            int(st["owner_chat_id"]),
            f"⚠️ Worker SHA mismatch after spawn: expected {expected_sha[:8]}, got {(observed_sha or 'unknown')[:8]}",
        )


def spawn_workers(n: int = 0) -> None:
    global _CTX, _EVENT_Q
    # Force fresh context to ensure workers use latest code
    _CTX = mp.get_context(_WORKER_START_METHOD)
    _EVENT_Q = _CTX.Queue()
    events_path = DRIVE_ROOT / "logs" / "events.jsonl"
    try:
        events_offset = int(events_path.stat().st_size)
    except Exception:
        events_offset = 0

    default_main = int(n or MAX_WORKERS or 1)
    main_count = max(0, int(os.environ.get("MAIN_WORKERS", str(default_main))))
    ext_enabled = str(os.environ.get("EXTERNAL_EXECUTORS_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
    codex_count = 0
    claude_count = 0
    if ext_enabled:
        if str(os.environ.get("CODEX_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}:
            codex_count = max(0, int(os.environ.get("CODEX_WORKERS", "0")))
        if str(os.environ.get("CLAUDE_CODE_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}:
            claude_count = max(0, int(os.environ.get("CLAUDE_CODE_WORKERS", "0")))
    worker_specs: List[Tuple[str, int]] = [("ouroboros", main_count), ("codex", codex_count), ("claude_code", claude_count)]
    count = sum(cnt for _, cnt in worker_specs)
    if count <= 0:
        worker_specs = [("ouroboros", 1)]
        count = 1
    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "worker_spawn_start",
            "start_method": _WORKER_START_METHOD,
            "count": count,
            "worker_specs": [{"kind": kind, "count": cnt} for kind, cnt in worker_specs],
        },
    )
    WORKERS.clear()
    i = 0
    for kind, cnt in worker_specs:
        for _ in range(cnt):
            in_q = _CTX.Queue()
            proc = _CTX.Process(target=worker_main,
                               args=(i, in_q, _EVENT_Q, str(REPO_DIR), str(DRIVE_ROOT)))
            proc.daemon = True
            proc.start()
            WORKERS[i] = Worker(wid=i, proc=proc, in_q=in_q, busy_task_id=None, kind=kind)
            i += 1
    global _LAST_SPAWN_TIME
    _LAST_SPAWN_TIME = time.time()
    # Run SHA verification in background to avoid blocking the main loop for up to 90s
    threading.Thread(target=_verify_worker_sha_after_spawn, args=(events_offset,), daemon=True).start()


def kill_workers(force: bool = False) -> None:
    from supervisor import queue
    with _queue_lock:
        cleared_running = len(RUNNING)
        for w in WORKERS.values():
            if w.proc.is_alive():
                w.proc.terminate()
        for w in WORKERS.values():
            w.proc.join(timeout=3)
        if force:
            _kill_survivors()
        WORKERS.clear()
        # --- Zombie prevention: write failure results before clearing ---
        try:
            orphaned_ids = []
            for task_id in list(RUNNING):
                try:
                    _write_failure_result(task_id)
                    orphaned_ids.append(task_id)
                except Exception:
                    log.warning("Failed to write failure result for running task %s", task_id, exc_info=True)
            drained = queue.drain_all_pending()
            drained_ids = []
            for task in drained:
                tid = task.get("id")
                if tid:
                    try:
                        _write_failure_result(tid)
                        drained_ids.append(tid)
                    except Exception:
                        log.warning("Failed to write failure result for pending task %s", tid, exc_info=True)
            if orphaned_ids or drained_ids:
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "zombie_prevention_cleanup",
                        "orphaned_running": orphaned_ids,
                        "drained_pending": drained_ids,
                    },
                )
        except Exception:
            log.warning("Zombie prevention cleanup failed", exc_info=True)
        RUNNING.clear()
    queue.persist_queue_snapshot(reason="kill_workers")
    if cleared_running:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "running_cleared_on_kill", "count": cleared_running,
                "force": force,
            },
        )


def _kill_survivors() -> None:
    """Force-kill any workers still alive after graceful termination."""
    from ouroboros.compat import force_kill_pid
    for w in WORKERS.values():
        if not w.proc.is_alive():
            continue
        pid = w.proc.pid
        if pid is None:
            continue
        force_kill_pid(pid)
        w.proc.join(timeout=2)


def respawn_worker(wid: int, kind: str = "ouroboros") -> None:
    global _LAST_SPAWN_TIME
    ctx = _get_ctx()
    in_q = ctx.Queue()
    proc = ctx.Process(target=worker_main,
                       args=(wid, in_q, get_event_q(), str(REPO_DIR), str(DRIVE_ROOT)))
    proc.daemon = True
    proc.start()
    WORKERS[wid] = Worker(wid=wid, proc=proc, in_q=in_q, busy_task_id=None, kind=kind)
    # Give freshly respawned workers the same init grace as startup workers.
    _LAST_SPAWN_TIME = time.time()


def assign_tasks() -> None:
    from supervisor import queue
    from supervisor.state import budget_remaining, EVOLUTION_BUDGET_RESERVE

    def _task_executor(task: Dict[str, Any]) -> str:
        return str(task.get("executor") or "ouroboros").strip().lower() or "ouroboros"

    def _worker_supports_task(worker: Worker, task: Dict[str, Any]) -> bool:
        return str(getattr(worker, "kind", "ouroboros") or "ouroboros").strip().lower() == _task_executor(task)

    def _fail_unroutable_task(task: Dict[str, Any], reason: str) -> None:
        task_id = str(task.get("id") or "")
        try:
            from ouroboros.task_results import STATUS_FAILED, write_task_result
            write_task_result(
                DRIVE_ROOT,
                task_id,
                STATUS_FAILED,
                parent_task_id=task.get("parent_task_id"),
                description=task.get("description"),
                context=task.get("context"),
                executor=_task_executor(task),
                task_kind=task.get("task_kind"),
                caller_class=task.get("caller_class"),
                model_policy=task.get("model_policy"),
                importance=task.get("importance"),
                defer_on_quota=bool(task.get("defer_on_quota", True)),
                budget_decision=task.get("budget_decision"),
                result=reason,
            )
        except Exception:
            log.warning("Failed to persist unroutable task failure for %s", task_id, exc_info=True)

        st = load_state()
        owner_chat_id = st.get("owner_chat_id")
        if owner_chat_id:
            send_with_budget(int(owner_chat_id), f"⚠️ Task rejected: {reason}")

    with _queue_lock:
        st = load_state()
        remaining = budget_remaining(st)
        
        if remaining <= 0:
            return  # Stop assigning ALL tasks if budget is completely exhausted
            
        for w in WORKERS.values():
            if w.busy_task_id is None and PENDING:
                # Find first suitable task (skip over-budget evolution tasks)
                chosen_idx = None
                for i, candidate in enumerate(PENDING):
                    if str(candidate.get("type") or "") == "evolution" and remaining < EVOLUTION_BUDGET_RESERVE:
                        continue
                    if not _worker_supports_task(w, candidate):
                        continue
                    chosen_idx = i
                    break
                if chosen_idx is None:
                    continue
                task = PENDING.pop(chosen_idx)
                w.busy_task_id = task["id"]
                w.in_q.put(task)
                now_ts = time.time()
                RUNNING[task["id"]] = {
                    "task": dict(task), "worker_id": w.wid,
                    "started_at": now_ts, "last_heartbeat_at": now_ts,
                    "soft_sent": False, "attempt": int(task.get("_attempt") or 1),
                }
                task_type = str(task.get("type") or "")
                if task_type in ("evolution", "review"):
                    st = load_state()
                    if st.get("owner_chat_id"):
                        emoji = '🧬' if task_type == 'evolution' else '🔎'
                        send_with_budget(
                            int(st["owner_chat_id"]),
                            f"{emoji} {task_type.capitalize()} task {task['id']} started.",
                        )
                queue.persist_queue_snapshot(reason="assign_task")

        # Explicitly fail tasks that cannot be routed to any configured worker kind.
        # This prevents silent fallback where a task marked executor=codex/claude_code
        # gets executed by an ouroboros worker.
        available_kinds = {
            str(getattr(w, "kind", "ouroboros") or "ouroboros").strip().lower()
            for w in WORKERS.values()
        }
        if available_kinds:
            unroutable: List[Dict[str, Any]] = []
            still_pending: List[Dict[str, Any]] = []
            for task in PENDING:
                if _task_executor(task) in available_kinds:
                    still_pending.append(task)
                else:
                    unroutable.append(task)
            if unroutable:
                PENDING[:] = still_pending
                for task in unroutable:
                    requested_executor = _task_executor(task)
                    reason = (
                        f"executor '{requested_executor}' is not routable: no worker pool "
                        f"with matching capability is active (available: {', '.join(sorted(available_kinds))})."
                    )
                    _fail_unroutable_task(task, reason)
                queue.persist_queue_snapshot(reason="unroutable_executor_tasks")


# ---------------------------------------------------------------------------
# Health + crash storm
# ---------------------------------------------------------------------------

def ensure_workers_healthy() -> None:
    from supervisor import queue
    # Grace period: skip health check right after spawn — workers need time to initialize
    alive_now = sum(1 for w in WORKERS.values() if w.proc.is_alive())
    if (time.time() - _LAST_SPAWN_TIME) < _SPAWN_GRACE_SEC and alive_now > 0:
        return
    busy_crashes = 0
    dead_detections = 0
    crashed_tasks = []
    for wid, w in list(WORKERS.items()):
        if not w.proc.is_alive():
            dead_detections += 1
            if w.busy_task_id is not None:
                busy_crashes += 1
            exitcode = w.proc.exitcode
            meta = RUNNING.get(w.busy_task_id, {}) if w.busy_task_id else {}
            task_info = meta.get("task", {}) if isinstance(meta, dict) else {}
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "worker_dead_detected",
                    "worker_id": wid,
                    "exitcode": exitcode,
                    "busy_task_id": w.busy_task_id,
                    "task_type": task_info.get("type") if isinstance(task_info, dict) else None,
                    "task_description": (task_info.get("description", "") or "")[:200] if isinstance(task_info, dict) else None,
                    "uptime_sec": round(time.time() - meta["started_at"]) if isinstance(meta, dict) and meta.get("started_at") else None,
                    "attempt": meta.get("attempt") if isinstance(meta, dict) else None,
                    "signal": -exitcode if isinstance(exitcode, int) and exitcode < 0 else None,
                },
            )
            if w.busy_task_id and isinstance(meta, dict) and meta.get("task"):
                crashed_tasks.append({"task_id": w.busy_task_id, "task_type": task_info.get("type") if isinstance(task_info, dict) else None})
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "worker_crash_task_dump",
                        "worker_id": wid,
                        "task": meta["task"],
                        "started_at": meta.get("started_at"),
                        "last_heartbeat_at": meta.get("last_heartbeat_at"),
                        "attempt": meta.get("attempt"),
                    },
                )
            if w.busy_task_id and w.busy_task_id in RUNNING:
                meta = RUNNING.pop(w.busy_task_id) or {}
                task = meta.get("task") if isinstance(meta, dict) else None
                if isinstance(task, dict):
                    try:
                        from ouroboros.task_results import STATUS_INTERRUPTED, write_task_result
                        write_task_result(
                            DRIVE_ROOT, str(w.busy_task_id), STATUS_INTERRUPTED,
                            result="Worker process died mid-task. Task will be retried.",
                        )
                    except Exception:
                        log.debug("Failed to write interrupted status for %s", w.busy_task_id, exc_info=True)
                    queue.enqueue_task(task, front=True)
            respawn_worker(wid, getattr(w, "kind", "ouroboros"))
            queue.persist_queue_snapshot(reason="worker_respawn_after_crash")

    now = time.time()
    if dead_detections:
        # Count only meaningful failures:
        # - any crash while a task was running, or
        # - all workers dead at once.
        if busy_crashes > 0 or alive_now == 0:
            CRASH_TS.extend([now] * max(1, dead_detections))
        else:
            # Idle worker deaths with at least one healthy worker are degraded mode,
            # not a crash storm condition.
            CRASH_TS.clear()

    CRASH_TS[:] = [t for t in CRASH_TS if (now - t) < 60.0]
    if len(CRASH_TS) >= 3:
        # Log crash storm but DON'T execv restart — that creates infinite loops.
        # Instead: kill dead workers, notify owner, continue with direct-chat (threading).
        st = load_state()
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "crash_storm_detected",
                "crash_count": len(CRASH_TS),
                "worker_count": len(WORKERS),
                "crashed_tasks": crashed_tasks,
            },
        )
        if st.get("owner_chat_id"):
            send_with_budget(
                int(st["owner_chat_id"]),
                "⚠️ Frequent worker crashes. Multiprocessing workers disabled, "
                "continuing in direct-chat mode (threading).",
            )
        # Kill all workers — direct chat via handle_chat_direct still works
        kill_workers()
        CRASH_TS.clear()
