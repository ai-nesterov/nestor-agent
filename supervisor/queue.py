"""
Supervisor — Task queue management.

Queue operations, priority, timeouts, persistence, evolution/review scheduling.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import threading
import time
import uuid
from glob import glob
from typing import Any, Dict, List, Optional, Tuple

from supervisor.state import (
    load_state, save_state, append_jsonl, atomic_write_text,
    QUEUE_SNAPSHOT_PATH, budget_pct, TOTAL_BUDGET_LIMIT,
    budget_remaining, EVOLUTION_BUDGET_RESERVE,
)
from supervisor.message_bus import send_with_budget
from ouroboros.evolution_objectives import select_next_objective

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DRIVE_ROOT: pathlib.Path = pathlib.Path.home() / "Ouroboros" / "data"
SOFT_TIMEOUT_SEC: int = 600
HARD_TIMEOUT_SEC: int = 1800
HEARTBEAT_STALE_SEC: int = 120
QUEUE_MAX_RETRIES: int = 1


def init(drive_root: pathlib.Path, soft_timeout: int, hard_timeout: int) -> None:
    global DRIVE_ROOT, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC
    DRIVE_ROOT = drive_root
    SOFT_TIMEOUT_SEC = soft_timeout
    HARD_TIMEOUT_SEC = hard_timeout


# ---------------------------------------------------------------------------
# Queue data structures (references to workers module globals)
# ---------------------------------------------------------------------------
# These will be set by workers.init_queue_refs()
PENDING: List[Dict[str, Any]] = []
RUNNING: Dict[str, Dict[str, Any]] = {}
QUEUE_SEQ_COUNTER_REF: Dict[str, int] = {"value": 0}

# Lock for all mutations to PENDING, RUNNING, WORKERS shared collections.
# Protects against concurrent access from main loop, direct-chat threads, watchdog.
_queue_lock = threading.RLock()


def init_queue_refs(pending: List[Dict[str, Any]], running: Dict[str, Dict[str, Any]],
                    seq_counter_ref: Dict[str, int]) -> None:
    """Called by workers.py to provide references to queue data structures."""
    global PENDING, RUNNING, QUEUE_SEQ_COUNTER_REF
    PENDING = pending
    RUNNING = running
    QUEUE_SEQ_COUNTER_REF = seq_counter_ref


# ---------------------------------------------------------------------------
# Queue priority
# ---------------------------------------------------------------------------

def _task_priority(task_type: str) -> int:
    t = str(task_type or "").strip().lower()
    if t in ("task", "review"):
        return 0
    if t == "evolution":
        return 1
    return 2


def _queue_sort_key(task: Dict[str, Any]) -> Tuple[int, int]:
    _pr = task.get("priority")
    pr = int(_pr) if _pr is not None else _task_priority(str(task.get("type") or ""))
    _seq = task.get("_queue_seq")
    seq = int(_seq) if _seq is not None else 0
    return pr, seq


def sort_pending() -> None:
    """Sort PENDING queue by priority."""
    PENDING.sort(key=_queue_sort_key)


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def drain_all_pending() -> list:
    """Remove and return all pending tasks. Used during crash storm cleanup.

    Caller must already hold _queue_lock (called from kill_workers which holds it).
    """
    drained = list(PENDING)
    PENDING.clear()
    persist_queue_snapshot(reason="drain_all_pending")
    return drained


def enqueue_task(task: Dict[str, Any], front: bool = False) -> Dict[str, Any]:
    """Add task to PENDING queue."""
    t = dict(task)
    QUEUE_SEQ_COUNTER_REF["value"] += 1
    seq = QUEUE_SEQ_COUNTER_REF["value"]
    t.setdefault("priority", _task_priority(str(t.get("type") or "")))
    _att = t.get("_attempt")
    t.setdefault("_attempt", int(_att) if _att is not None else 1)
    t["_queue_seq"] = -seq if front else seq
    t["queued_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    PENDING.append(t)
    sort_pending()
    return t


def reconcile_orphaned_scheduled_results(max_age_sec: int = 180, scan_limit: int = 300) -> int:
    """Fail scheduled task-results that are no longer present in queue state.

    This guards against rare races (e.g. duplicate supervisors) where a task can
    remain `scheduled` indefinitely despite being absent from both PENDING/RUNNING.
    """
    from ouroboros.task_results import STATUS_FAILED, write_task_result

    now = time.time()
    fixed = 0
    with _queue_lock:
        pending_ids = {str(t.get("id") or "") for t in PENDING if isinstance(t, dict)}
        running_ids = {str(tid or "") for tid in RUNNING.keys()}
        active_ids = pending_ids.union(running_ids)

    results_dir = DRIVE_ROOT / "task_results"
    if not results_dir.exists():
        return 0

    files = sorted(
        glob(str(results_dir / "*.json")),
        key=lambda p: pathlib.Path(p).stat().st_mtime if pathlib.Path(p).exists() else 0.0,
        reverse=True,
    )[: max(1, int(scan_limit))]

    for fp in files:
        path = pathlib.Path(fp)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("status") or "").strip().lower() != "scheduled":
            continue
        task_id = str(payload.get("task_id") or path.stem)
        if not task_id or task_id in active_ids:
            continue
        ts = parse_iso_to_ts(str(payload.get("ts") or "")) or path.stat().st_mtime
        age_sec = max(0.0, now - float(ts))
        if age_sec < float(max_age_sec):
            continue

        reason = (
            "Task marked failed as orphaned: status=scheduled but task is missing "
            "from pending/running queues."
        )
        try:
            write_task_result(
                DRIVE_ROOT,
                task_id,
                STATUS_FAILED,
                parent_task_id=payload.get("parent_task_id"),
                description=payload.get("description"),
                context=payload.get("context"),
                executor=payload.get("executor"),
                repo_scope=payload.get("repo_scope") or [],
                constraints=payload.get("constraints") or {},
                artifact_policy=payload.get("artifact_policy"),
                quota_class=payload.get("quota_class"),
                priority=payload.get("priority"),
                task_type=payload.get("task_type") or payload.get("type"),
                task_kind=payload.get("task_kind"),
                caller_class=payload.get("caller_class"),
                model_policy=payload.get("model_policy"),
                model_override=payload.get("model_override"),
                importance=payload.get("importance"),
                defer_on_quota=bool(payload.get("defer_on_quota", True)),
                budget_decision=payload.get("budget_decision"),
                result=reason,
                trace_summary="orphaned_scheduled_task_detected",
                cost_usd=float(payload.get("cost_usd") or 0.0),
                total_rounds=int(payload.get("total_rounds") or 0),
            )
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "orphaned_scheduled_task_failed",
                    "task_id": task_id,
                    "age_sec": round(age_sec, 1),
                },
            )
            fixed += 1
        except Exception:
            log.warning("Failed to reconcile orphaned scheduled task %s", task_id, exc_info=True)
    return fixed


def queue_has_task_type(task_type: str) -> bool:
    """Check if a task of given type exists in PENDING or RUNNING."""
    tt = str(task_type or "")
    if any(str(t.get("type") or "") == tt for t in PENDING):
        return True
    for meta in RUNNING.values():
        task = meta.get("task") if isinstance(meta, dict) else None
        if isinstance(task, dict) and str(task.get("type") or "") == tt:
            return True
    return False


def persist_queue_snapshot(reason: str = "") -> None:
    """Save PENDING and RUNNING to snapshot file."""
    pending_rows = []
    for t in PENDING:
        pending_rows.append({
            "id": t.get("id"), "type": t.get("type"), "priority": t.get("priority"),
            "attempt": t.get("_attempt"), "queued_at": t.get("queued_at"),
            "queue_seq": t.get("_queue_seq"),
            "task": {
                "id": t.get("id"), "type": t.get("type"), "chat_id": t.get("chat_id"),
                "text": t.get("text"), "priority": t.get("priority"),
                "depth": t.get("depth"), "description": t.get("description"),
                "context": t.get("context"), "parent_task_id": t.get("parent_task_id"),
                "executor": t.get("executor"), "executor_mode": t.get("executor_mode"),
                "repo_scope": t.get("repo_scope"), "constraints": t.get("constraints"),
                "artifact_policy": t.get("artifact_policy"), "quota_class": t.get("quota_class"),
                "task_kind": t.get("task_kind"), "caller_class": t.get("caller_class"),
                "model_policy": t.get("model_policy"), "model_override": t.get("model_override"),
                "importance": t.get("importance"), "defer_on_quota": t.get("defer_on_quota"),
                "budget_decision": t.get("budget_decision"),
                "_attempt": t.get("_attempt"), "review_reason": t.get("review_reason"),
                "review_source_task_id": t.get("review_source_task_id"),
            },
        })
    running_rows = []
    now = time.time()
    for task_id, meta in RUNNING.items():
        task = meta.get("task") if isinstance(meta, dict) else {}
        started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
        hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
        running_rows.append({
            "id": task_id, "type": task.get("type"), "priority": task.get("priority"),
            "attempt": meta.get("attempt"), "worker_id": meta.get("worker_id"),
            "runtime_sec": round(max(0.0, now - started), 2) if started > 0 else 0.0,
            "heartbeat_lag_sec": round(max(0.0, now - hb), 2) if hb > 0 else None,
            "soft_sent": bool(meta.get("soft_sent")), "task": task,
        })
    payload = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": reason,
        "pending_count": len(PENDING), "running_count": len(RUNNING),
        "pending": pending_rows, "running": running_rows,
    }
    try:
        atomic_write_text(QUEUE_SNAPSHOT_PATH, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        log.warning("Failed to persist queue snapshot (reason=%s)", reason, exc_info=True)
        pass


def parse_iso_to_ts(iso_ts: str) -> Optional[float]:
    """Parse ISO timestamp to Unix timestamp."""
    txt = str(iso_ts or "").strip()
    if not txt:
        return None
    try:
        return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        log.debug("Failed to parse ISO timestamp: %s", txt, exc_info=True)
        return None


def restore_pending_from_snapshot(max_age_sec: int = 900) -> int:
    """Restore PENDING queue from snapshot file."""
    if PENDING:
        return 0
    try:
        if not QUEUE_SNAPSHOT_PATH.exists():
            return 0
        snap = json.loads(QUEUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return 0
        ts = str(snap.get("ts") or "")
        ts_unix = parse_iso_to_ts(ts)
        if ts_unix is None:
            return 0
        if (time.time() - ts_unix) > max_age_sec:
            return 0
        restored = 0
        for row in (snap.get("pending") or []):
            task = row.get("task") if isinstance(row, dict) else None
            if not isinstance(task, dict):
                continue
            if not task.get("id") or not task.get("chat_id"):
                continue
            enqueue_task(task)
            restored += 1
        if restored > 0:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "queue_restored_from_snapshot",
                    "restored_pending": restored,
                },
            )
            persist_queue_snapshot(reason="queue_restored")
        return restored
    except Exception:
        log.warning("Failed to restore pending queue from snapshot", exc_info=True)
        return 0


def cancel_task_by_id(task_id: str) -> bool:
    """Cancel a task by ID (from PENDING or RUNNING)."""
    from supervisor import workers

    with _queue_lock:
        for i, t in enumerate(list(PENDING)):
            if t["id"] == task_id:
                PENDING.pop(i)
                try:
                    from ouroboros.task_results import STATUS_CANCELLED, write_task_result
                    write_task_result(
                        DRIVE_ROOT, task_id, STATUS_CANCELLED,
                        result="Task cancelled by user/agent request.",
                    )
                except Exception:
                    pass
                persist_queue_snapshot(reason="cancel_pending")
                return True

        for w in workers.WORKERS.values():
            if w.busy_task_id == task_id:
                RUNNING.pop(task_id, None)
                try:
                    from ouroboros.task_results import STATUS_CANCELLED, write_task_result
                    write_task_result(
                        DRIVE_ROOT, task_id, STATUS_CANCELLED,
                        result="Running task cancelled and worker terminated.",
                    )
                except Exception:
                    pass
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
                workers.respawn_worker(w.wid)
                persist_queue_snapshot(reason="cancel_running")
                return True
    return False


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------

def enforce_task_timeouts() -> None:
    """Check all RUNNING tasks for timeouts and enforce them."""
    # Import here to avoid circular dependency during module load
    from supervisor import workers
    
    if not RUNNING:
        return
    now = time.time()
    st = load_state()
    owner_chat_id = int(st.get("owner_chat_id") or 0)

    for task_id, meta in list(RUNNING.items()):
        if not isinstance(meta, dict):
            continue
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        started_at = float(meta.get("started_at") or 0.0)
        if started_at <= 0:
            continue
        last_hb = float(meta.get("last_heartbeat_at") or started_at)
        runtime_sec = max(0.0, now - started_at)
        hb_lag_sec = max(0.0, now - last_hb)
        hb_stale = hb_lag_sec >= HEARTBEAT_STALE_SEC
        _wid = meta.get("worker_id")
        worker_id = int(_wid) if _wid is not None else -1
        task_type = str(task.get("type") or "")
        _att = meta.get("attempt")
        if _att is None:
            _att = task.get("_attempt")
        attempt = int(_att) if _att is not None else 1

        if runtime_sec >= SOFT_TIMEOUT_SEC and not bool(meta.get("soft_sent")):
            meta["soft_sent"] = True
            if owner_chat_id:
                send_with_budget(
                    owner_chat_id,
                    f"⏱️ Task {task_id} running for {int(runtime_sec)}s. "
                    f"type={task_type}, heartbeat_lag={int(hb_lag_sec)}s. Continuing.",
                )

        if runtime_sec < HARD_TIMEOUT_SEC:
            continue

        RUNNING.pop(task_id, None)
        if worker_id in workers.WORKERS and workers.WORKERS[worker_id].busy_task_id == task_id:
            workers.WORKERS[worker_id].busy_task_id = None

        if worker_id in workers.WORKERS:
            w = workers.WORKERS[worker_id]
            try:
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
            except Exception:
                log.warning("Failed to terminate worker %d during hard timeout", worker_id, exc_info=True)
                pass
            workers.respawn_worker(worker_id)

        try:
            from ouroboros.task_results import STATUS_FAILED, write_task_result
            write_task_result(
                DRIVE_ROOT, task_id, STATUS_FAILED,
                result=f"Task killed by hard timeout after {int(runtime_sec)}s.",
            )
        except Exception:
            pass

        requeued = False
        new_attempt = attempt
        if attempt <= QUEUE_MAX_RETRIES and isinstance(task, dict):
            retried = dict(task)
            retried["original_task_id"] = task_id
            retried["id"] = uuid.uuid4().hex[:8]
            retried["_attempt"] = attempt + 1
            retried["timeout_retry_from"] = task_id
            retried["timeout_retry_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            enqueue_task(retried, front=True)
            requeued = True
            new_attempt = attempt + 1

        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "task_hard_timeout",
                "task_id": task_id, "task_type": task_type,
                "worker_id": worker_id, "runtime_sec": round(runtime_sec, 2),
                "heartbeat_lag_sec": round(hb_lag_sec, 2), "heartbeat_stale": hb_stale,
                "attempt": attempt, "requeued": requeued, "new_attempt": new_attempt,
                "max_retries": QUEUE_MAX_RETRIES,
            },
        )

        if owner_chat_id:
            if requeued:
                send_with_budget(owner_chat_id, (
                    f"🛑 Hard-timeout: task {task_id} killed after {int(runtime_sec)}s.\n"
                    f"Worker {worker_id} restarted. Task queued for retry attempt={new_attempt}."
                ))
            else:
                send_with_budget(owner_chat_id, (
                    f"🛑 Hard-timeout: task {task_id} killed after {int(runtime_sec)}s.\n"
                    f"Worker {worker_id} restarted. Retry limit exhausted, task stopped."
                ))

        persist_queue_snapshot(reason="task_hard_timeout")


# ---------------------------------------------------------------------------
# Evolution + review scheduling
# ---------------------------------------------------------------------------

def build_evolution_task_text(cycle: int, objective: Dict[str, Any] | None = None) -> str:
    """Build evolution task text for autonomous cycles.

    FAILURE PATTERN: The system gets stuck in "report_only" mode - repeatedly
    calling wait_for_task(), checking status, running diagnostics - but never
    producing concrete file changes. This is a CRITICAL BUG in the task itself.
    
    If you call wait_for_task() more than ONCE - you are already failing.
    If you call run_shell() for diagnostics (logs, ps, git status) without
    immediately following with file changes - you are failing.
    If you send progress/status messages without having modified files - you are failing.
    
    CORRECT BEHAVIOR:
    - First tool call: read ONE file that needs improvement
    - Second tool call: make ONE concrete change (repo_write, str_replace_editor)
    - Third tool call: commit (repo_commit)
    - If done, return success. If stuck, fail early with SPECIFIC question.
    
    DO NOT: plan, analyze, think about, check status, wait, reflect.
    DO: read file, change file, commit.
    """
    objective = objective if isinstance(objective, dict) else {}
    description = str(objective.get("description") or "Make one small, concrete improvement to the agent runtime.").strip()
    hypothesis = str(objective.get("hypothesis") or "A concrete repository improvement is better than idle reflection.").strip()
    subsystem = str(objective.get("subsystem") or "runtime").strip()
    acceptance_checks = [str(item).strip() for item in (objective.get("acceptance_checks") or []) if str(item).strip()]
    if not acceptance_checks:
        acceptance_checks = [
            "Modify a repository file",
            "Create a repo_commit",
            "Explain the concrete improvement achieved",
        ]
    acceptance_lines = "".join(f"- {item}\n" for item in acceptance_checks)
    return (
        f"EVOLUTION #{cycle}\n\n"
        "Autonomous cycle.\n\n"
        "ROLE:\n"
        "- evolution_implementer\n\n"
        "OBJECTIVE:\n"
        f"- {description}\n\n"
        "HYPOTHESIS:\n"
        f"- {hypothesis}\n\n"
        "TARGET SUBSYSTEM:\n"
        f"- {subsystem}\n\n"
        "ACCEPTANCE:\n"
        f"{acceptance_lines}\n"
        "INSTRUCTIONS:\n"
        "1. Read the relevant repository file(s) for the objective\n"
        "2. Make ONE concrete repository change that advances the objective\n"
        "3. Commit the change\n\n"
        "RULES:\n"
        "- First tool call MUST be repo_read or str_replace_editor\n"
        "- Second tool call MUST be repo_write, str_replace_editor, or repo_commit\n"
        "- If you call wait_for_task() - you are already failing\n"
        "- If you call run_shell() for diagnostics only - you are already failing\n"
        "- Progress/status messages without file changes = FAILURE\n\n"
        "TIMING:\n"
        "- You have 3 minutes max. After 3 minutes without a commit, FAIL.\n"
        "- If you can't find anything to improve in 3 minutes, return NEEDS_OWNER_INPUT\n"
        "  with ONE specific question. \"I don't know what to do\" = FAILURE.\n\n"
        "SUCCESS CRITERIA:\n"
        "- Modified files exist (repo_write/str_replace_editor)\n"
        "- Git commit created via repo_commit\n"
        "- Nothing else counts as success.\n\n"
        "FAILURE OUTCOMES (use when stuck):\n"
        "- FAILED: something broke, can't complete\n"
        "- NEEDS_OWNER_INPUT: specific question only (not \"what should I do\")\n"
    )


def build_evolution_plan_task_text(
    cycle: int,
    objective: Dict[str, Any] | None = None,
) -> str:
    objective = objective if isinstance(objective, dict) else {}
    description = str(objective.get("description") or "Plan the next concrete evolution step.").strip()
    hypothesis = str(objective.get("hypothesis") or "").strip()
    subsystem = str(objective.get("subsystem") or "runtime").strip()
    acceptance_checks = [str(item).strip() for item in (objective.get("acceptance_checks") or []) if str(item).strip()]
    acceptance_lines = "".join(f"- {item}\n" for item in acceptance_checks) or "- Produce a concrete implementable plan.\n"
    return (
        f"EVOLUTION PLAN #{cycle}\n\n"
        "ROLE:\n"
        "- evolution_planner\n\n"
        "OBJECTIVE:\n"
        f"- {description}\n\n"
        + (f"HYPOTHESIS:\n- {hypothesis}\n\n" if hypothesis else "")
        + "TARGET SUBSYSTEM:\n"
        f"- {subsystem}\n\n"
        "ACCEPTANCE:\n"
        f"{acceptance_lines}\n"
        "INSTRUCTIONS:\n"
        "1. Produce a short plan for one concrete repository mutation.\n"
        "2. Name likely target files and the minimal intended change.\n"
        "3. Include a validation idea.\n"
        "4. End with these headings exactly:\n"
        "PLAN_SUMMARY:\n"
        "TARGET_FILES:\n"
        "VALIDATION:\n"
        "5. Do not modify files in this planning step.\n"
    )


def build_evolution_verify_task_text(
    candidate_task_id: str,
    objective: Dict[str, Any] | None = None,
) -> str:
    objective = objective if isinstance(objective, dict) else {}
    description = str(objective.get("description") or "Verify the latest evolution candidate.").strip()
    hypothesis = str(objective.get("hypothesis") or "").strip()
    acceptance_checks = [str(item).strip() for item in (objective.get("acceptance_checks") or []) if str(item).strip()]
    acceptance_lines = "".join(f"- {item}\n" for item in acceptance_checks) or "- The candidate should satisfy its stated acceptance checks.\n"
    return (
        "EVOLUTION VERIFIER\n\n"
        "ROLE:\n"
        "- evolution_verifier\n\n"
        f"CANDIDATE TASK ID: {candidate_task_id}\n\n"
        "OBJECTIVE:\n"
        f"- {description}\n"
        + (f"\nHYPOTHESIS:\n- {hypothesis}\n" if hypothesis else "\n")
        + "VERIFICATION CHECKS:\n"
        + acceptance_lines
        + "\nINSTRUCTIONS:\n"
        + "1. Inspect the candidate task result and repository state.\n"
        + "2. Run the smallest relevant verification you can justify (tests, git diff, targeted inspection).\n"
        + "3. Do not modify repository files in the verifier task.\n"
        + "4. End your final answer with exactly one verifier verdict line:\n"
        + "   VERIFIER_DECISION: ACCEPTED\n"
        + "   or VERIFIER_DECISION: REJECTED\n"
        + "   or VERIFIER_DECISION: NEEDS_OWNER_INPUT\n"
        + "5. Add a short REASON line immediately after the verdict.\n"
    )


def build_review_task_text(reason: str) -> str:
    """Build review task text.

    Includes explicit Constitution-compliance mandate so the reviewer treats
    the Constitution as the supreme authority, not just another file in context.
    """
    safe_reason = (reason or "owner request").replace("\n", " ").strip()[:400]
    return (
        "IMPORTANT — Constitutional Compliance Check:\n"
        "The Constitution (in your system prompt) is the supreme authority above all code, "
        "prompts, and conventions. Every finding and recommendation in this review MUST be "
        "verified against its principles. Specifically:\n"
        "- Flag any code, architecture, or behavior that contradicts a constitutional principle.\n"
        "- When recommending changes, cite which principle supports the recommendation.\n"
        "- If a trade-off exists between principles, apply the priority order: P0 > P1 > P2 > ... > P8.\n"
        "- Do NOT recommend anything that would violate the Constitution, even if it seems "
        "technically beneficial.\n"
        "Constitutional compliance is not a separate section — it must permeate every part of the review.\n\n"
        "---\n\n"
        f"REVIEW TASK: {safe_reason}"
    )


def queue_review_task(reason: str, force: bool = False) -> Optional[str]:
    """Queue a review task."""
    st = load_state()
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return None
    if (not force) and queue_has_task_type("review"):
        return None
    tid = uuid.uuid4().hex[:8]
    enqueue_task({
        "id": tid, "type": "review",
        "chat_id": int(owner_chat_id),
        "text": build_review_task_text(reason=reason),
    })
    persist_queue_snapshot(reason="review_enqueued")
    send_with_budget(int(owner_chat_id), f"🔎 Review queued: {tid} ({reason})")
    return tid


def queue_has_task_kind(task_kind: str) -> bool:
    """Check if a task of given kind exists in PENDING or RUNNING."""
    tk = str(task_kind or "")
    if any(str(t.get("task_kind") or "") == tk for t in PENDING):
        return True
    for meta in RUNNING.values():
        task = meta.get("task") if isinstance(meta, dict) else None
        if isinstance(task, dict) and str(task.get("task_kind") or "") == tk:
            return True
    return False


def _get_evolution_cooldown(outcome: str) -> int:
    """Get cooldown duration for a given evolution outcome.
    
    Reads from OUROBOROS_EVOLUTION_COOLDOWN_MAP env var (JSON dict)
    or falls back to sensible defaults.
    
    Default mapping:
    - "failed": 60s (short cooldown to allow retry after transient failures)
    - "blocked_external": 600s (10 min for external blocks)
    - "no_actionable_goal": 1800s (30 min for idle states)
    - "needs_owner_input": 0 (wait for owner message, handled separately)
    - Default for unknown outcomes: 120s
    """
    # "needs_owner_input" is handled by the waiting_for_owner logic, not cooldown
    if outcome == "needs_owner_input":
        return 0
    
    default_cooldown_map = {
        "failed": 60,
        "blocked_external": 600,
        "no_actionable_goal": 1800,
    }
    
    # Try to read from environment
    raw_map = os.environ.get("OUROBOROS_EVOLUTION_COOLDOWN_MAP")
    if raw_map:
        try:
            custom_map = json.loads(raw_map)
            if isinstance(custom_map, dict):
                return int(custom_map.get(outcome, default_cooldown_map.get(outcome, 120)))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    
    # Fall back to defaults
    return int(default_cooldown_map.get(outcome, 120))


def enqueue_evolution_task_if_needed() -> None:
    """Enqueue the evolution planning stage if the queue is empty and evolution mode is enabled.

    Circuit breaker: pauses evolution after 3 consecutive failures to prevent
    burning budget on infinite retry loops.
    Also checks for pending user tasks - don't trigger evolution if there
    are actual tasks waiting to be processed.
    """
    if PENDING or RUNNING:
        return
    st = load_state()
    if not bool(st.get("evolution_mode_enabled")):
        return
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return

    # Circuit breaker: check for consecutive evolution failures
    consecutive_failures = int(st.get("evolution_consecutive_failures") or 0)
    if consecutive_failures >= 3:
        st["evolution_mode_enabled"] = False
        save_state(st)
        log.warning(f"🧬⚠️ Evolution paused: {consecutive_failures} consecutive failures. "
                   f"Use /evolve start to resume.")
        send_with_budget(
            int(owner_chat_id),
            f"🧬⚠️ Evolution paused: {consecutive_failures} consecutive failures. "
            f"Use /evolve start to resume after investigating the issue."
        )
        return

    outcome = str(st.get("evolution_last_outcome") or "").strip().lower()
    outcome_ts = parse_iso_to_ts(str(st.get("evolution_last_outcome_at") or ""))
    owner_msg_ts = parse_iso_to_ts(str(st.get("last_owner_message_at") or ""))
    if bool(st.get("evolution_waiting_for_owner")):
        if owner_msg_ts is None or outcome_ts is None or owner_msg_ts <= outcome_ts:
            return
        st["evolution_waiting_for_owner"] = False
        st["evolution_blocked_reason"] = ""
        save_state(st)

    cooldown_sec = _get_evolution_cooldown(outcome)
    if cooldown_sec > 0 and outcome_ts is not None and (time.time() - outcome_ts) < cooldown_sec:
        return

    remaining = budget_remaining(st)
    if remaining < EVOLUTION_BUDGET_RESERVE:
        st["evolution_mode_enabled"] = False
        save_state(st)
        send_with_budget(int(owner_chat_id), f"💸 Evolution stopped: ${remaining:.2f} remaining (reserve ${EVOLUTION_BUDGET_RESERVE:.0f} for conversations).")
        return

    cycle = int(st.get("evolution_cycle") or 0) + 1
    tid = uuid.uuid4().hex[:8]
    objective = select_next_objective(DRIVE_ROOT, state=st)
    enqueue_task({
        "id": tid,
        "type": "task",
        "task_kind": "evolution_plan",
        "chat_id": int(owner_chat_id),
        "text": build_evolution_plan_task_text(cycle, objective=objective),
        "description": str(objective.get("description") or ""),
        "context": str(objective.get("hypothesis") or ""),
        "agent_role": "evolution_planner",
        "evolution_cycle": cycle,
        "objective_id": str(objective.get("id") or ""),
        "objective_source": str(objective.get("source") or ""),
        "objective_subsystem": str(objective.get("subsystem") or ""),
        "objective_hypothesis": str(objective.get("hypothesis") or ""),
        "acceptance_checks": list(objective.get("acceptance_checks") or []),
    })
    st["evolution_cycle"] = cycle
    st["last_evolution_task_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(st)
    send_with_budget(int(owner_chat_id), f"🧬 Evolution #{cycle}: {tid}")
