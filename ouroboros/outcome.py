"""Canonical execution outcome schema for Ouroboros tasks."""

from __future__ import annotations

import json
import os
from typing import Any, Dict


OUTCOME_EXECUTED_WORK = "executed_work"
OUTCOME_SCHEDULED_FOLLOWUP = "scheduled_followup"
OUTCOME_COMMITTED = "committed"
OUTCOME_VERIFIED_NO_CHANGE = "verified_no_change_needed"
OUTCOME_NEEDS_OWNER_INPUT = "needs_owner_input"
OUTCOME_BLOCKED_EXTERNAL = "blocked_external"
OUTCOME_REPORT_ONLY = "report_only"
OUTCOME_FAILED = "failed"

OUTCOME_SOURCE_RULE = "rule"
OUTCOME_SOURCE_MODEL = "model"
OUTCOME_SOURCE_FALLBACK_SUPERVISOR = "fallback_supervisor"

ALL_OUTCOME_CLASSES = frozenset({
    OUTCOME_EXECUTED_WORK,
    OUTCOME_SCHEDULED_FOLLOWUP,
    OUTCOME_COMMITTED,
    OUTCOME_VERIFIED_NO_CHANGE,
    OUTCOME_NEEDS_OWNER_INPUT,
    OUTCOME_BLOCKED_EXTERNAL,
    OUTCOME_REPORT_ONLY,
    OUTCOME_FAILED,
})

PRODUCTIVE_OUTCOME_CLASSES = frozenset({
    OUTCOME_EXECUTED_WORK,
    OUTCOME_SCHEDULED_FOLLOWUP,
    OUTCOME_COMMITTED,
    OUTCOME_VERIFIED_NO_CHANGE,
})

NONPRODUCTIVE_OUTCOME_CLASSES = frozenset({
    OUTCOME_NEEDS_OWNER_INPUT,
    OUTCOME_BLOCKED_EXTERNAL,
    OUTCOME_REPORT_ONLY,
    OUTCOME_FAILED,
})

_OWNER_INPUT_PATTERNS = (
    "what would you like me to do",
    "what should i work on",
    "what's the goal",
    "what goal should i",
    "want me to proceed",
    "which goal should i work on",
    "which option should i",
    "what do you want me to",
    "что выбираешь",
    "что скажешь",
    "что бы ты хотел",
    "хочешь, чтобы я",
    "какая цель",
    "какую цель",
)
_BLOCKED_PATTERNS = (
    "all models are down",
    "provider is back",
    "provider errors",
    "returned no response",
    "quota blocked",
    "soft limited",
    "hard blocked",
)


def default_execution_facts() -> Dict[str, Any]:
    """Stable structure for runtime execution facts."""
    return {
        "tool_calls_total": 0,
        "tool_errors_total": 0,
        "rounds_total": 0,
        "assistant_messages_total": 0,
        "final_text_present": False,
        "final_text_length": 0,
        "scheduled_task_calls": 0,
        "wait_for_task_calls": 0,
        "get_task_result_calls": 0,
        "apply_task_patch_calls": 0,
        "validate_executor_result_calls": 0,
        "repo_commit_calls": 0,
        "write_ops_total": 0,
        "read_ops_total": 0,
        "mutating_tools": [],
        "owner_message_requests": 0,
        "provider_blocked": False,
        "fallback_exhausted": False,
        "empty_model_responses": 0,
        "review_blocked_seen": False,
        "executor_used": "",
        "subtasks_spawned": 0,
        "subtasks_completed_productively": 0,
    }


def build_execution_outcome(
    outcome_class: str = OUTCOME_FAILED,
    *,
    reason: str = "",
    source: str = OUTCOME_SOURCE_RULE,
    productive: bool | None = None,
) -> Dict[str, Any]:
    normalized = str(outcome_class or OUTCOME_FAILED).strip().lower()
    if normalized not in ALL_OUTCOME_CLASSES:
        normalized = OUTCOME_FAILED
    if productive is None:
        productive = normalized in PRODUCTIVE_OUTCOME_CLASSES
    return {
        "outcome_class": normalized,
        "outcome_reason": str(reason or "").strip(),
        "outcome_source": str(source or OUTCOME_SOURCE_RULE).strip().lower(),
        "productive": bool(productive),
    }


def _requests_owner_direction(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(pattern in lowered for pattern in _OWNER_INPUT_PATTERNS)


def classify_outcome_from_facts(
    *,
    task_type: str,
    execution_facts: Dict[str, Any],
    final_text: str,
) -> Dict[str, Any]:
    """Deterministic first-pass classification from runtime facts."""
    facts = execution_facts if isinstance(execution_facts, dict) else default_execution_facts()
    text = str(final_text or "").strip()
    lowered = text.lower()

    if bool(facts.get("provider_blocked")) or bool(facts.get("fallback_exhausted")):
        return build_execution_outcome(
            OUTCOME_BLOCKED_EXTERNAL,
            reason="provider_or_fallback_blocked",
        )

    if any(pattern in lowered for pattern in _BLOCKED_PATTERNS):
        return build_execution_outcome(
            OUTCOME_BLOCKED_EXTERNAL,
            reason="provider_or_quota_block_message",
        )

    if _requests_owner_direction(text) or int(facts.get("owner_message_requests") or 0) > 0:
        return build_execution_outcome(
            OUTCOME_NEEDS_OWNER_INPUT,
            reason="agent_requested_owner_direction",
        )

    if int(facts.get("repo_commit_calls") or 0) > 0:
        return build_execution_outcome(
            OUTCOME_COMMITTED,
            reason="repo_commit_executed",
        )

    if int(facts.get("scheduled_task_calls") or 0) > 0:
        return build_execution_outcome(
            OUTCOME_SCHEDULED_FOLLOWUP,
            reason="followup_task_scheduled",
        )

    if list(facts.get("mutating_tools") or []) or int(facts.get("write_ops_total") or 0) > 0:
        return build_execution_outcome(
            OUTCOME_EXECUTED_WORK,
            reason="mutating_tool_executed",
        )

    if int(facts.get("tool_errors_total") or 0) > 0 and int(facts.get("tool_calls_total") or 0) == 0:
        return build_execution_outcome(
            OUTCOME_FAILED,
            reason="tool_errors_without_progress",
            productive=False,
        )

    if not text:
        return build_execution_outcome(
            OUTCOME_FAILED,
            reason="empty_final_text",
            productive=False,
        )

    if int(facts.get("tool_calls_total") or 0) == 0:
        return build_execution_outcome(
            OUTCOME_REPORT_ONLY,
            reason="text_only_completion_without_tool_execution",
            productive=False,
        )

    if str(task_type or "").strip().lower() == "review":
        return build_execution_outcome(
            OUTCOME_VERIFIED_NO_CHANGE,
            reason="review_completed_without_mutation",
        )

    return build_execution_outcome(
        OUTCOME_REPORT_ONLY,
        reason="non_mutating_completion_requires_adjudication",
        productive=False,
    )


def maybe_adjudicate_outcome_with_model(
    *,
    task_type: str,
    execution_facts: Dict[str, Any],
    final_text: str,
    deterministic_outcome: Dict[str, Any],
) -> Dict[str, Any]:
    """Use a small model only for ambiguous semantic cases."""
    outcome_class = str((deterministic_outcome or {}).get("outcome_class") or "").strip().lower()
    outcome_reason = str((deterministic_outcome or {}).get("outcome_reason") or "").strip().lower()
    if outcome_class != OUTCOME_REPORT_ONLY or outcome_reason != "non_mutating_completion_requires_adjudication":
        return deterministic_outcome

    if str(os.environ.get("OUROBOROS_ENABLE_OUTCOME_ADJUDICATION", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
        return deterministic_outcome

    facts = execution_facts if isinstance(execution_facts, dict) else default_execution_facts()
    prompt = (
        "Classify this Ouroboros task completion. Use only one of these labels: "
        "verified_no_change_needed, needs_owner_input, blocked_external, report_only.\n"
        "Return strict JSON with keys outcome_class and outcome_reason.\n\n"
        f"task_type={str(task_type or '').strip().lower() or 'task'}\n"
        f"execution_facts={json.dumps(facts, ensure_ascii=False, sort_keys=True)}\n"
        f"final_text={json.dumps(str(final_text or '')[:2000], ensure_ascii=False)}\n"
    )

    try:
        from ouroboros.config import get_lane_model, use_local_for_lane
        from ouroboros.llm import DEFAULT_LIGHT_MODEL, LLMClient

        use_local_light = use_local_for_lane("LIGHT")
        light_model = get_lane_model("LIGHT", prefer_local=use_local_light) or DEFAULT_LIGHT_MODEL
        client = LLMClient()
        msg, _usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=200,
            use_local=use_local_light,
        )
        content = str((msg or {}).get("content") or "").strip()
        if not content:
            return deterministic_outcome
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return deterministic_outcome
        payload = json.loads(content[start:end + 1])
        candidate = str(payload.get("outcome_class") or "").strip().lower()
        reason = str(payload.get("outcome_reason") or "").strip()
        if candidate not in {
            OUTCOME_VERIFIED_NO_CHANGE,
            OUTCOME_NEEDS_OWNER_INPUT,
            OUTCOME_BLOCKED_EXTERNAL,
            OUTCOME_REPORT_ONLY,
        }:
            return deterministic_outcome
        return build_execution_outcome(
            candidate,
            reason=reason or "model_adjudicated_ambiguous_completion",
            source=OUTCOME_SOURCE_MODEL,
        )
    except Exception:
        return deterministic_outcome


def apply_task_type_outcome_policy(
    *,
    task_type: str,
    execution_outcome: Dict[str, Any],
    final_text: str = "",
    execution_facts: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Apply task-type-specific lifecycle policy on top of generic outcome classes."""
    outcome = dict(execution_outcome or {})
    current = str(outcome.get("outcome_class") or "").strip().lower()
    normalized_task_type = str(task_type or "").strip().lower()
    facts = execution_facts if isinstance(execution_facts, dict) else {}
    text = str(final_text or "")

    if normalized_task_type == "evolution":
        if _requests_owner_direction(text):
            return build_execution_outcome(
                OUTCOME_NEEDS_OWNER_INPUT,
                reason="agent_requested_owner_direction",
                source=outcome.get("outcome_source") or OUTCOME_SOURCE_RULE,
                productive=False,
            )
        if current == OUTCOME_REPORT_ONLY:
            return build_execution_outcome(
                OUTCOME_FAILED,
                reason="evolution_requires_concrete_work_not_report_only",
                source=outcome.get("outcome_source") or OUTCOME_SOURCE_RULE,
                productive=False,
            )
        mutating_tools = {str(item).strip().lower() for item in (facts.get("mutating_tools") or []) if str(item).strip()}
        if current == OUTCOME_EXECUTED_WORK and mutating_tools == {"knowledge_write"}:
            return build_execution_outcome(
                OUTCOME_FAILED,
                reason="evolution_knowledge_only_write_not_sufficient",
                source=outcome.get("outcome_source") or OUTCOME_SOURCE_RULE,
                productive=False,
            )

    if normalized_task_type == "review":
        if current == OUTCOME_REPORT_ONLY:
            return build_execution_outcome(
                OUTCOME_VERIFIED_NO_CHANGE,
                reason="review_completed_without_required_modification",
                source=outcome.get("outcome_source") or OUTCOME_SOURCE_RULE,
            )

    return outcome
