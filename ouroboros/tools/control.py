"""Control tools: restart, promote, schedule, cancel, review, chat_history, update_scratchpad, switch_model."""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.config import get_lane_model, use_local_for_lane
from ouroboros.task_results import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_REJECTED_DUPLICATE,
    STATUS_REQUESTED,
    load_task_result,
    write_task_result,
)
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, write_text, run_cmd

log = logging.getLogger(__name__)

MAX_SUBTASK_DEPTH = 3
_ALLOWED_EXECUTORS = {"ouroboros", "claude_code", "codex"}
_ALLOWED_ARTIFACT_POLICIES = {"patch_only", "keep_worktree"}
_ALLOWED_QUOTA_CLASSES = {"cheap", "expensive"}
_ALLOWED_MODEL_POLICIES = {"cheap", "balanced", "premium", "critical"}
_ALLOWED_IMPORTANCE = {"low", "medium", "high", "critical"}
_ALLOWED_BUDGET_DECISIONS = {"auto", "defer", "force_run"}


def _emit_control_event(ctx: ToolContext, evt: Dict[str, Any]) -> bool:
    """Emit control-plane events immediately when event_queue is available.

    Returns True when event was delivered to supervisor queue immediately.
    Caller should append to pending_events only when this returns False.
    """
    q = getattr(ctx, "event_queue", None)
    if q is None:
        return False
    try:
        put_nowait = getattr(q, "put_nowait", None)
        if callable(put_nowait):
            put_nowait(evt)
        else:
            q.put(evt)
        return True
    except Exception:
        log.debug("Immediate control event emit failed; falling back to pending_events", exc_info=True)
        return False


def _request_restart(ctx: ToolContext, reason: str) -> str:
    if str(ctx.current_task_type or "") == "evolution" and not ctx.last_push_succeeded:
        return "⚠️ RESTART_BLOCKED: in evolution mode, commit+push first."
    # Persist expected SHA for post-restart verification
    try:
        sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=ctx.repo_dir)
        branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ctx.repo_dir)
        verify_path = ctx.drive_path("state") / "pending_restart_verify.json"
        write_text(verify_path, json.dumps({
            "ts": utc_now_iso(), "expected_sha": sha,
            "expected_branch": branch, "reason": reason,
        }, ensure_ascii=False, indent=2))
    except Exception:
        log.debug("Failed to read VERSION file or git ref for restart verification", exc_info=True)
        pass
    ctx.pending_events.append({"type": "restart_request", "reason": reason, "ts": utc_now_iso()})
    ctx.last_push_succeeded = False
    return f"Restart requested: {reason}"


def _promote_to_stable(ctx: ToolContext, reason: str) -> str:
    ctx.pending_events.append({"type": "promote_to_stable", "reason": reason, "ts": utc_now_iso()})
    return f"Promote to stable requested: {reason}"


def _schedule_task(
    ctx: ToolContext,
    description: str,
    context: str = "",
    parent_task_id: str = "",
    executor: str = "ouroboros",
    repo_scope: List[str] | None = None,
    constraints: Dict[str, Any] | None = None,
    artifact_policy: str = "patch_only",
    quota_class: str = "cheap",
    priority: int = 0,
    task_type: str = "task",
    task_kind: str = "general",
    caller_class: str = "",
    model_policy: str = "balanced",
    model_override: str = "",
    importance: str = "medium",
    defer_on_quota: bool = True,
    budget_decision: str = "auto",
) -> str:
    current_depth = getattr(ctx, 'task_depth', 0)
    new_depth = current_depth + 1
    if new_depth > MAX_SUBTASK_DEPTH:
        return f"ERROR: Subtask depth limit ({MAX_SUBTASK_DEPTH}) exceeded. Simplify your approach."

    if getattr(ctx, 'is_direct_chat', False):
        from ouroboros.utils import append_jsonl
        try:
            append_jsonl(ctx.drive_logs() / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "schedule_task_from_direct_chat",
                "description": description[:200],
                "warning": "schedule_task called from direct chat context — potential duplicate work",
            })
        except Exception:
            pass

    executor_value = str(executor or "ouroboros").strip().lower()
    if executor_value not in _ALLOWED_EXECUTORS:
        return f"ERROR: Unknown executor '{executor}'. Allowed: {', '.join(sorted(_ALLOWED_EXECUTORS))}"
    artifact_policy_value = str(artifact_policy or "patch_only").strip().lower()
    if artifact_policy_value not in _ALLOWED_ARTIFACT_POLICIES:
        return (
            f"ERROR: Unknown artifact_policy '{artifact_policy}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_ARTIFACT_POLICIES))}"
        )
    quota_class_value = str(quota_class or "cheap").strip().lower()
    if quota_class_value not in _ALLOWED_QUOTA_CLASSES:
        return (
            f"ERROR: Unknown quota_class '{quota_class}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_QUOTA_CLASSES))}"
        )

    model_policy_value = str(model_policy or "balanced").strip().lower()
    if model_policy_value not in _ALLOWED_MODEL_POLICIES:
        return (
            f"ERROR: Unknown model_policy '{model_policy}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_MODEL_POLICIES))}"
        )
    importance_value = str(importance or "medium").strip().lower()
    if importance_value not in _ALLOWED_IMPORTANCE:
        return (
            f"ERROR: Unknown importance '{importance}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_IMPORTANCE))}"
        )
    budget_decision_value = str(budget_decision or "auto").strip().lower()
    if budget_decision_value not in _ALLOWED_BUDGET_DECISIONS:
        return (
            f"ERROR: Unknown budget_decision '{budget_decision}'. "
            f"Allowed: {', '.join(sorted(_ALLOWED_BUDGET_DECISIONS))}"
        )
    task_type_value = str(task_type or "task").strip().lower() or "task"
    task_kind_value = str(task_kind or "general").strip().lower() or "general"
    caller_class_value = str(caller_class or "").strip().lower()
    if not caller_class_value:
        if str(getattr(ctx, "current_task_type", "")).lower() == "consciousness":
            caller_class_value = "consciousness"
        elif str(getattr(ctx, "current_task_type", "")).lower() == "review":
            caller_class_value = "review"
        else:
            caller_class_value = "main_task_agent"

    safe_repo_scope = [str(p) for p in (repo_scope or []) if str(p).strip()]
    safe_constraints = dict(constraints or {})

    tid = uuid.uuid4().hex[:8]
    evt = {
        "type": "schedule_task",
        "description": description,
        "task_id": tid,
        "depth": new_depth,
        "ts": utc_now_iso(),
        "executor": executor_value,
        "repo_scope": safe_repo_scope,
        "constraints": safe_constraints,
        "artifact_policy": artifact_policy_value,
        "quota_class": quota_class_value,
        "priority": int(priority),
        "task_type": task_type_value,
        "task_kind": task_kind_value,
        "caller_class": caller_class_value,
        "model_policy": model_policy_value,
        "model_override": str(model_override or "").strip(),
        "importance": importance_value,
        "defer_on_quota": bool(defer_on_quota),
        "budget_decision": budget_decision_value,
    }
    if context:
        evt["context"] = context
    if parent_task_id:
        evt["parent_task_id"] = parent_task_id
    if not _emit_control_event(ctx, evt):
        ctx.pending_events.append(evt)
    try:
        write_task_result(
            ctx.drive_root,
            tid,
            STATUS_REQUESTED,
            parent_task_id=parent_task_id or None,
            description=description,
            context=context,
            executor=executor_value,
            repo_scope=safe_repo_scope,
            constraints=safe_constraints,
            artifact_policy=artifact_policy_value,
            quota_class=quota_class_value,
            priority=int(priority),
            task_type=task_type_value,
            task_kind=task_kind_value,
            caller_class=caller_class_value,
            model_policy=model_policy_value,
            model_override=str(model_override or "").strip(),
            importance=importance_value,
            defer_on_quota=bool(defer_on_quota),
            budget_decision=budget_decision_value,
            result="Task request queued. Awaiting supervisor acceptance.",
        )
    except Exception:
        log.warning("Failed to persist requested task status for %s", tid, exc_info=True)
    return f"Task request queued {tid} ({executor_value}): {description}"


def _cancel_task(ctx: ToolContext, task_id: str) -> str:
    evt = {"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()}
    if not _emit_control_event(ctx, evt):
        ctx.pending_events.append(evt)
    return f"Cancel requested: {task_id}"


def _request_review(ctx: ToolContext, reason: str) -> str:
    evt = {"type": "review_request", "reason": reason, "ts": utc_now_iso()}
    if not _emit_control_event(ctx, evt):
        ctx.pending_events.append(evt)
    return f"Review requested: {reason}"


def _chat_history(ctx: ToolContext, count: int = 100, offset: int = 0, search: str = "") -> str:
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    return mem.chat_history(count=count, offset=offset, search=search)


def _update_scratchpad(ctx: ToolContext, content: str) -> str:
    """LLM-driven scratchpad update — appends a timestamped block (Constitution P3: LLM-first)."""
    if not content or not isinstance(content, str) or len(content.strip()) < 10:
        return (
            "⚠️ REJECTED: content is empty or too short "
            f"(got {type(content).__name__}, len={len(content) if isinstance(content, str) else 'N/A'}). "
            "Scratchpad must have meaningful content (10+ chars). "
            "This likely means the tool call was malformed — check your arguments."
        )
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    mem.ensure_files()
    block = mem.append_scratchpad_block(content, source="task")
    return f"OK: scratchpad block appended ({len(content)} chars, ts={block.get('ts', '?')[:16]})"


def _send_user_message(ctx: ToolContext, text: str, reason: str = "") -> str:
    """Send a proactive message to the user (not as reply to a task).

    Use when you have something genuinely worth saying — an insight,
    a question, a status update, or an invitation to collaborate.
    """
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send proactive message."
    if not text or not text.strip():
        return "⚠️ Empty message."

    from ouroboros.utils import append_jsonl
    ctx.pending_events.append({
        "type": "send_message",
        "chat_id": ctx.current_chat_id,
        "text": text,
        "format": "markdown",
        "is_progress": False,
        "ts": utc_now_iso(),
    })
    append_jsonl(ctx.drive_logs() / "events.jsonl", {
        "ts": utc_now_iso(),
        "type": "proactive_message",
        "reason": reason,
        "text_preview": text[:200],
    })
    return "OK: message queued for delivery."


def _update_identity(ctx: ToolContext, content: str) -> str:
    """Update identity manifest (who you are, who you want to become)."""
    if not content or not isinstance(content, str) or len(content.strip()) < 50:
        return (
            "⚠️ REJECTED: content is empty or too short "
            f"(got {type(content).__name__}, len={len(content) if isinstance(content, str) else 'N/A'}). "
            "Identity must be a substantial text (50+ chars). "
            "This likely means the tool call was malformed — check your arguments."
        )
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    mem.ensure_files()

    old_content = ""
    path = ctx.drive_root / "memory" / "identity.md"
    if path.exists():
        try:
            old_content = path.read_text(encoding="utf-8")
        except Exception:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    mem.append_identity_journal({
        "ts": utc_now_iso(),
        "old_len": len(old_content),
        "new_len": len(content),
        "old_preview": old_content[:500],
        "new_preview": content[:500],
    })

    return f"OK: identity updated ({len(content)} chars)"


def _toggle_evolution(ctx: ToolContext, enabled: bool) -> str:
    """Toggle evolution mode on/off via supervisor event."""
    ctx.pending_events.append({
        "type": "toggle_evolution",
        "enabled": bool(enabled),
        "ts": utc_now_iso(),
    })
    state_str = "ON" if enabled else "OFF"
    return f"OK: evolution mode toggled {state_str}."


def _toggle_consciousness(ctx: ToolContext, action: str = "status") -> str:
    """Control background consciousness: start, stop, or status."""
    ctx.pending_events.append({
        "type": "toggle_consciousness",
        "action": action,
        "ts": utc_now_iso(),
    })
    return f"OK: consciousness '{action}' requested."


def _switch_model(ctx: ToolContext, model: str = "", effort: str = "") -> str:
    """LLM-driven model/effort switch (Constitution P3: LLM-first).

    Stored in ToolContext, applied on the next LLM call in the loop.
    """
    from ouroboros.llm import LLMClient, normalize_reasoning_effort
    available = LLMClient().available_models()
    changes = []

    if model:
        if model not in available:
            return f"⚠️ Unknown model: {model}. Available: {', '.join(available)}"
        ctx.active_model_override = model
        
        use_local = False
        if model == get_lane_model("MAIN") and use_local_for_lane("MAIN"):
            use_local = True
        elif model == get_lane_model("CODE") and use_local_for_lane("CODE"):
            use_local = True
        elif model == get_lane_model("LIGHT") and use_local_for_lane("LIGHT"):
            use_local = True
        elif model == get_lane_model("FALLBACK") and use_local_for_lane("FALLBACK"):
            use_local = True
            
        ctx.active_use_local_override = use_local
        changes.append(f"model={model}{' (local)' if use_local else ''}")

    if effort:
        normalized = normalize_reasoning_effort(effort, default="medium")
        ctx.active_effort_override = normalized
        changes.append(f"effort={normalized}")

    if not changes:
        return f"Current available models: {', '.join(available)}. Pass model and/or effort to switch."

    return f"OK: switching to {', '.join(changes)} on next round."


def _get_task_result(ctx: ToolContext, task_id: str) -> str:
    """Read the result of a completed subtask."""
    data = load_task_result(ctx.drive_root, task_id)
    if not data:
        return f"Task {task_id}: unknown or not yet registered"
    status = data.get("status", "unknown")
    result = data.get("result", "")
    cost = data.get("cost_usd", 0)
    trace = data.get("trace_summary", "")
    executor = str(data.get("executor") or "ouroboros")
    changed_files = data.get("changed_files") or []
    diff_stat = data.get("diff_stat") or {}
    artifact_dir = data.get("artifact_dir")
    tests_run = data.get("tests_run") or []
    tests_passed = data.get("tests_passed")
    if status == STATUS_COMPLETED:
        output = (
            f"Task {task_id} [{status}]: executor={executor}, cost=${cost:.2f}\n\n"
            f"[BEGIN_SUBTASK_OUTPUT]\n{result}\n[END_SUBTASK_OUTPUT]"
        )
    elif status == STATUS_REJECTED_DUPLICATE:
        duplicate_of = str(data.get("duplicate_of") or "?")
        output = (
            f"Task {task_id} [{status}]: duplicate_of={duplicate_of}\n\n"
            f"{result or f'Task was rejected as a duplicate of {duplicate_of}.'}"
        )
    else:
        output = f"Task {task_id} [{status}]: executor={executor} {result or 'No details available.'}"
    if changed_files:
        output += f"\n\n[CHANGED_FILES]\n" + "\n".join(f"- {p}" for p in changed_files)
    if diff_stat:
        output += (
            "\n\n[DIFF_STAT]\n"
            f"files={int(diff_stat.get('files') or 0)}, "
            f"insertions={int(diff_stat.get('insertions') or 0)}, "
            f"deletions={int(diff_stat.get('deletions') or 0)}"
        )
    if tests_run or tests_passed is not None:
        output += f"\n\n[TESTS]\npassed={tests_passed}\nrun={tests_run}"
    if artifact_dir:
        output += f"\n\n[ARTIFACT_DIR]\n{artifact_dir}\n[IMPORT_HINT]\nUse apply_task_patch(task_id=\"{task_id}\") after validate_executor_result."
    if trace:
        output += f"\n\n[SUBTASK_TRACE]\n{trace}\n[/SUBTASK_TRACE]"
    return output


def _wait_for_task(ctx: ToolContext, task_id: str) -> str:
    """Check if a subtask has completed. Call repeatedly to poll."""
    return _get_task_result(ctx, task_id)


def _resume_deferred_tasks(ctx: ToolContext, limit: int = 20) -> str:
    evt = {"type": "resume_deferred_tasks", "limit": int(limit), "ts": utc_now_iso()}
    if not _emit_control_event(ctx, evt):
        ctx.pending_events.append(evt)
    return f"Resume deferred tasks requested (limit={int(limit)})."


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("request_restart", {
            "name": "request_restart",
            "description": "Ask supervisor to restart runtime (after successful push).",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        }, _request_restart),
        ToolEntry("promote_to_stable", {
            "name": "promote_to_stable",
            "description": "Promote ouroboros -> ouroboros-stable. Call when you consider the code stable.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        }, _promote_to_stable),
        ToolEntry("schedule_task", {
            "name": "schedule_task",
            "description": "Schedule a background task. Returns task_id for later retrieval. Use executor='ouroboros' by default; claude_code for architecture-heavy refactors; codex for deterministic implementation-heavy subtasks.",
            "parameters": {"type": "object", "properties": {
                "description": {"type": "string", "description": "Task description — be specific about scope and expected deliverable"},
                "context": {"type": "string", "description": "Optional context from parent task: background info, constraints, style guide, etc."},
                "parent_task_id": {"type": "string", "description": "Optional parent task ID for tracking lineage"},
                "executor": {"type": "string", "enum": ["ouroboros", "claude_code", "codex"], "default": "ouroboros"},
                "repo_scope": {"type": "array", "items": {"type": "string"}, "description": "Optional path scopes relevant to this task"},
                "constraints": {"type": "object", "description": "Execution constraints for worker runtime"},
                "artifact_policy": {"type": "string", "enum": ["patch_only", "keep_worktree"], "default": "patch_only"},
                "quota_class": {"type": "string", "enum": ["cheap", "expensive"], "default": "cheap"},
                "priority": {"type": "integer", "default": 0},
                "task_type": {"type": "string", "default": "task", "description": "queue task type: task|review|evolution"},
                "task_kind": {"type": "string", "default": "general", "description": "semantic task class: general|review_plan|review_code|refactor_plan|evolution_plan|implement"},
                "caller_class": {"type": "string", "default": "main_task_agent", "description": "caller class for policy routing"},
                "model_policy": {"type": "string", "enum": ["cheap", "balanced", "premium", "critical"], "default": "balanced"},
                "model_override": {"type": "string", "description": "optional explicit model pin for this task"},
                "importance": {"type": "string", "enum": ["low", "medium", "high", "critical"], "default": "medium"},
                "defer_on_quota": {"type": "boolean", "default": True, "description": "defer task instead of hard rejection when quota-policy blocks it"},
                "budget_decision": {"type": "string", "enum": ["auto", "defer", "force_run"], "default": "auto", "description": "agent decision for soft budget policy"},
            }, "required": ["description"]},
        }, _schedule_task),
        ToolEntry("cancel_task", {
            "name": "cancel_task",
            "description": "Cancel a task by ID.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        }, _cancel_task),
        ToolEntry("request_review", {
            "name": "request_review",
            "description": "Request a deep review of code, prompts, and state. You decide when a review is needed.",
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string", "description": "Why you want a review (context for the reviewer)"},
            }, "required": ["reason"]},
        }, _request_review),
        ToolEntry("chat_history", {
            "name": "chat_history",
            "description": "Retrieve messages from chat history. Supports search.",
            "parameters": {"type": "object", "properties": {
                "count": {"type": "integer", "default": 100, "description": "Number of messages (from latest)"},
                "offset": {"type": "integer", "default": 0, "description": "Skip N from end (pagination)"},
                "search": {"type": "string", "default": "", "description": "Text filter"},
            }, "required": []},
        }, _chat_history),
        ToolEntry("update_scratchpad", {
            "name": "update_scratchpad",
            "description": "Append a block to your working memory (scratchpad). Each call adds a "
                           "timestamped block; oldest blocks are auto-evicted when the cap (10) is reached. "
                           "Write what matters NOW — active tasks, decisions, observations. "
                           "Persists across sessions, read at every task start.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "Content for this scratchpad block"},
            }, "required": ["content"]},
        }, _update_scratchpad),
        ToolEntry("send_user_message", {
            "name": "send_user_message",
            "description": "Send a proactive message to the user. Use when you have something "
                           "genuinely worth saying — an insight, a question, or an invitation to collaborate. "
                           "This is NOT for task responses (those go automatically).",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "Message text"},
                "reason": {"type": "string", "description": "Why you're reaching out (logged, not sent)"},
            }, "required": ["text"]},
        }, _send_user_message),
        ToolEntry("update_identity", {
            "name": "update_identity",
            "description": "Update your identity manifest (who you are, who you want to become). "
                           "Persists across sessions. Obligation to yourself (Principle 1: Continuity). "
                           "Read your current identity first, then evolve it — add, refine, deepen. "
                           "Full rewrites are allowed but should be rare; continuity of self matters.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "Full identity content (prefer evolving over rewriting from scratch)"},
            }, "required": ["content"]},
        }, _update_identity),
        ToolEntry("toggle_evolution", {
            "name": "toggle_evolution",
            "description": "Enable or disable evolution mode. When enabled, Ouroboros runs continuous self-improvement cycles.",
            "parameters": {"type": "object", "properties": {
                "enabled": {"type": "boolean", "description": "true to enable, false to disable"},
            }, "required": ["enabled"]},
        }, _toggle_evolution),
        ToolEntry("toggle_consciousness", {
            "name": "toggle_consciousness",
            "description": "Control background consciousness: 'start', 'stop', or 'status'.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "status"], "description": "Action to perform"},
            }, "required": ["action"]},
        }, _toggle_consciousness),
        ToolEntry("switch_model", {
            "name": "switch_model",
            "description": "Switch to a different LLM model or reasoning effort level. "
                           "Use when you need more power (complex code, deep reasoning) "
                           "or want to save budget (simple tasks). Takes effect on next round.",
            "parameters": {"type": "object", "properties": {
                "model": {"type": "string", "description": "Model name (e.g. anthropic/claude-sonnet-4). Leave empty to keep current."},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"],
                           "description": "Reasoning effort level. Leave empty to keep current."},
            }, "required": []},
        }, _switch_model),
        ToolEntry("get_task_result", {
            "name": "get_task_result",
            "description": "Read the result of a completed subtask. Use after schedule_task to collect results.",
            "parameters": {"type": "object", "required": ["task_id"], "properties": {
                "task_id": {"type": "string", "description": "Task ID returned by schedule_task"},
            }},
        }, _get_task_result),
        ToolEntry("wait_for_task", {
            "name": "wait_for_task",
            "description": "Check if a subtask has completed. Returns result if done, or 'still running' message. Call repeatedly to poll.",
            "parameters": {"type": "object", "required": ["task_id"], "properties": {
                "task_id": {"type": "string", "description": "Task ID to check"},
            }},
        }, _wait_for_task),
        ToolEntry("resume_deferred_tasks", {
            "name": "resume_deferred_tasks",
            "description": "Attempt to re-admit deferred external tasks after quota reset or policy changes.",
            "parameters": {"type": "object", "required": [], "properties": {
                "limit": {"type": "integer", "default": 20},
            }},
        }, _resume_deferred_tasks),
    ]
