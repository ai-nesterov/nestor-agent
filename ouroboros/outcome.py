"""Canonical execution outcome schema for Ouroboros tasks."""

from __future__ import annotations

import json
import os
from typing import Any, Dict


OUTCOME_EXECUTED_WORK = "executed_work"
OUTCOME_SCHEDULED_FOLLOWUP = "scheduled_followup"
OUTCOME_COMMITTED = "committed"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REJECTED = "rejected"
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
    OUTCOME_ACCEPTED,
    OUTCOME_REJECTED,
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
    OUTCOME_ACCEPTED,
    OUTCOME_VERIFIED_NO_CHANGE,
})

NONPRODUCTIVE_OUTCOME_CLASSES = frozenset({
    OUTCOME_REJECTED,
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

_HIGH_IMPACT_MUTATION_TOOLS = frozenset({
    "str_replace_editor",
    "apply_task_patch",
    "repo_commit",
    "data_write",
})
_LOW_IMPACT_MUTATION_TOOLS = frozenset({
    "knowledge_write",
    "update_scratchpad",
})


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
    confidence: float | None = None,
) -> Dict[str, Any]:
    normalized = str(outcome_class or OUTCOME_FAILED).strip().lower()
    if normalized not in ALL_OUTCOME_CLASSES:
        normalized = OUTCOME_FAILED
    if productive is None:
        productive = normalized in PRODUCTIVE_OUTCOME_CLASSES
    payload = {
        "outcome_class": normalized,
        "outcome_reason": str(reason or "").strip(),
        "outcome_source": str(source or OUTCOME_SOURCE_RULE).strip().lower(),
        "productive": bool(productive),
    }
    if confidence is not None:
        try:
            payload["confidence"] = max(0.0, min(1.0, float(confidence)))
        except Exception:
            pass
    return payload


def _requests_owner_direction(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(pattern in lowered for pattern in _OWNER_INPUT_PATTERNS)


def _normalized_mutating_tools(execution_facts: Dict[str, Any]) -> set[str]:
    return {
        str(item).strip().lower()
        for item in (execution_facts or {}).get("mutating_tools") or []
        if str(item).strip()
    }


def mutation_impact_from_facts(execution_facts: Dict[str, Any]) -> str:
    facts = execution_facts if isinstance(execution_facts, dict) else {}
    tools = _normalized_mutating_tools(facts)
    if int(facts.get("repo_commit_calls") or 0) > 0 or (tools & _HIGH_IMPACT_MUTATION_TOOLS):
        return "high"
    if not tools and int(facts.get("write_ops_total") or 0) <= 0:
        return "none"
    if tools and tools.issubset(_LOW_IMPACT_MUTATION_TOOLS):
        return "low"
    if int(facts.get("write_ops_total") or 0) > 0:
        return "medium"
    return "none"


def derive_outcome_constraints_from_facts(
    *,
    task_type: str,
    execution_facts: Dict[str, Any],
    final_text: str,
) -> Dict[str, Any]:
    facts = execution_facts if isinstance(execution_facts, dict) else default_execution_facts()
    text = str(final_text or "").strip()
    normalized_task_type = str(task_type or "").strip().lower()
    impact = mutation_impact_from_facts(facts)

    if bool(facts.get("provider_blocked")) or bool(facts.get("fallback_exhausted")):
        return {
            "forced_outcome": OUTCOME_BLOCKED_EXTERNAL,
            "reason": "provider_or_fallback_blocked",
            "allowed_outcomes": {OUTCOME_BLOCKED_EXTERNAL},
            "semantic_adjudication": False,
        }

    if int(facts.get("owner_message_requests") or 0) > 0:
        return {
            "forced_outcome": OUTCOME_NEEDS_OWNER_INPUT,
            "reason": "agent_requested_owner_direction_marker",
            "allowed_outcomes": {OUTCOME_NEEDS_OWNER_INPUT},
            "semantic_adjudication": False,
            "default_outcome": OUTCOME_NEEDS_OWNER_INPUT,
            "default_reason": "agent_requested_owner_direction_marker",
        }

    if int(facts.get("repo_commit_calls") or 0) > 0:
        return {
            "forced_outcome": OUTCOME_COMMITTED,
            "reason": "repo_commit_executed",
            "allowed_outcomes": {OUTCOME_COMMITTED},
            "semantic_adjudication": False,
            "default_outcome": OUTCOME_COMMITTED,
            "default_reason": "repo_commit_executed",
        }

    if impact == "high":
        return {
            "forced_outcome": OUTCOME_EXECUTED_WORK,
            "reason": "high_impact_mutation_executed",
            "allowed_outcomes": {OUTCOME_EXECUTED_WORK},
            "semantic_adjudication": False,
            "default_outcome": OUTCOME_EXECUTED_WORK,
            "default_reason": "high_impact_mutation_executed",
        }

    if int(facts.get("scheduled_task_calls") or 0) > 0 and impact == "none":
        return {
            "forced_outcome": OUTCOME_SCHEDULED_FOLLOWUP,
            "reason": "followup_task_scheduled",
            "allowed_outcomes": {OUTCOME_SCHEDULED_FOLLOWUP},
            "semantic_adjudication": False,
            "default_outcome": OUTCOME_SCHEDULED_FOLLOWUP,
            "default_reason": "followup_task_scheduled",
        }

    if int(facts.get("tool_errors_total") or 0) > 0 and int(facts.get("tool_calls_total") or 0) == 0 and not text:
        return {
            "forced_outcome": OUTCOME_FAILED,
            "reason": "tool_errors_without_progress",
            "allowed_outcomes": {OUTCOME_FAILED},
            "semantic_adjudication": False,
            "default_outcome": OUTCOME_FAILED,
            "default_reason": "tool_errors_without_progress",
        }

    if not text:
        return {
            "forced_outcome": OUTCOME_FAILED,
            "reason": "empty_final_text",
            "allowed_outcomes": {OUTCOME_FAILED},
            "semantic_adjudication": False,
            "default_outcome": OUTCOME_FAILED,
            "default_reason": "empty_final_text",
        }

    # Evolution tasks: report_only is never acceptable.
    # Any actual repo mutation = EXECUTED_WORK. Low-impact only (scratchpad/identity) = FAILED.
    if normalized_task_type == "evolution":
        if impact == "high":
            pass  # already caught above: forced OUTCOME_EXECUTED_WORK
        elif _requests_owner_direction(text):
            return {
                "forced_outcome": OUTCOME_NEEDS_OWNER_INPUT,
                "reason": "agent_requested_owner_direction",
                "allowed_outcomes": {OUTCOME_NEEDS_OWNER_INPUT},
                "semantic_adjudication": False,
                "default_outcome": OUTCOME_NEEDS_OWNER_INPUT,
                "default_reason": "agent_requested_owner_direction",
            }
        elif impact in {"low", "medium"}:
            # Deterministic: no report_only. Low-only mutations (scratchpad, identity)
            # without repo changes = FAILED. Repo mutations = EXECUTED_WORK.
            tools = _normalized_mutating_tools(facts)
            repo_mutations = tools - {"knowledge_write", "update_scratchpad", "update_identity"}
            if repo_mutations:
                return {
                    "forced_outcome": OUTCOME_EXECUTED_WORK,
                    "reason": "evolution_repo_mutation_detected",
                    "allowed_outcomes": {OUTCOME_EXECUTED_WORK},
                    "semantic_adjudication": False,
                    "default_outcome": OUTCOME_EXECUTED_WORK,
                    "default_reason": "evolution_repo_mutation_detected",
                }
            else:
                return {
                    "forced_outcome": OUTCOME_FAILED,
                    "reason": "evolution_knowledge_only_write_insufficient",
                    "allowed_outcomes": {OUTCOME_FAILED},
                    "semantic_adjudication": False,
                    "default_outcome": OUTCOME_FAILED,
                    "default_reason": "evolution_knowledge_only_write_insufficient",
                }

    if int(facts.get("tool_calls_total") or 0) == 0:
        # Evolution with zero tool calls = deterministic FAILED. No report_only.
        if normalized_task_type == "evolution":
            return {
                "forced_outcome": OUTCOME_FAILED,
                "reason": "evolution_zero_tool_calls",
                "allowed_outcomes": {OUTCOME_FAILED},
                "semantic_adjudication": False,
                "default_outcome": OUTCOME_FAILED,
                "default_reason": "evolution_zero_tool_calls",
            }
        allowed = {
            OUTCOME_NEEDS_OWNER_INPUT,
            OUTCOME_REPORT_ONLY,
            OUTCOME_FAILED,
        }
        if normalized_task_type != "evolution":
            allowed.add(OUTCOME_VERIFIED_NO_CHANGE)
        default_outcome = OUTCOME_VERIFIED_NO_CHANGE if normalized_task_type == "review" else OUTCOME_REPORT_ONLY
        default_reason = "text_only_completion_requires_adjudication"
        return {
            "forced_outcome": "",
            "reason": "text_only_completion_requires_adjudication",
            "allowed_outcomes": allowed,
            "semantic_adjudication": True,
            "default_outcome": default_outcome,
            "default_reason": default_reason,
        }

    if normalized_task_type == "review":
        return {
            "forced_outcome": "",
            "reason": "review_semantic_adjudication_required",
            "allowed_outcomes": {
                OUTCOME_VERIFIED_NO_CHANGE,
                OUTCOME_REPORT_ONLY,
                OUTCOME_NEEDS_OWNER_INPUT,
                OUTCOME_FAILED,
            },
            "semantic_adjudication": True,
            "default_outcome": OUTCOME_VERIFIED_NO_CHANGE,
            "default_reason": "review_semantic_adjudication_required",
        }

    if impact in {"low", "medium"}:
        allowed = {
            OUTCOME_EXECUTED_WORK,
            OUTCOME_NEEDS_OWNER_INPUT,
            OUTCOME_REPORT_ONLY,
            OUTCOME_FAILED,
        }
        if normalized_task_type != "evolution":
            allowed.add(OUTCOME_VERIFIED_NO_CHANGE)
        return {
            "forced_outcome": "",
            "reason": f"{impact}_impact_mutation_requires_adjudication",
            "allowed_outcomes": allowed,
            "semantic_adjudication": True,
            "default_outcome": OUTCOME_REPORT_ONLY,
            "default_reason": f"{impact}_impact_mutation_requires_adjudication",
        }

    return {
        "forced_outcome": OUTCOME_REPORT_ONLY,
        "reason": "non_mutating_completion_requires_adjudication",
        "allowed_outcomes": {OUTCOME_REPORT_ONLY},
        "semantic_adjudication": False,
        "default_outcome": OUTCOME_REPORT_ONLY,
        "default_reason": "non_mutating_completion_requires_adjudication",
    }


def classify_outcome_from_facts(
    *,
    task_type: str,
    execution_facts: Dict[str, Any],
    final_text: str,
) -> Dict[str, Any]:
    """Deterministic first-pass classification from runtime facts."""
    constraints = derive_outcome_constraints_from_facts(
        task_type=task_type,
        execution_facts=execution_facts,
        final_text=final_text,
    )
    forced_outcome = str(constraints.get("forced_outcome") or "").strip().lower()
    reason = str(constraints.get("reason") or "").strip()
    if forced_outcome:
        return build_execution_outcome(
            forced_outcome,
            reason=reason,
            productive=forced_outcome in PRODUCTIVE_OUTCOME_CLASSES,
        )
    default_outcome = str(constraints.get("default_outcome") or OUTCOME_REPORT_ONLY).strip().lower()
    default_reason = str(constraints.get("default_reason") or reason or "semantic_adjudication_required").strip()
    return build_execution_outcome(
        default_outcome,
        reason=default_reason,
        productive=default_outcome in PRODUCTIVE_OUTCOME_CLASSES,
    )


def resolve_outcome_conflict(
    *,
    constraints: Dict[str, Any],
    candidate_outcome: Dict[str, Any],
) -> Dict[str, Any]:
    forced_outcome = str((constraints or {}).get("forced_outcome") or "").strip().lower()
    allowed_outcomes = {
        str(item).strip().lower()
        for item in ((constraints or {}).get("allowed_outcomes") or set())
        if str(item).strip()
    }
    candidate_class = str((candidate_outcome or {}).get("outcome_class") or "").strip().lower()
    if forced_outcome:
        return build_execution_outcome(
            forced_outcome,
            reason=str((constraints or {}).get("reason") or forced_outcome),
            source=OUTCOME_SOURCE_RULE,
            productive=forced_outcome in PRODUCTIVE_OUTCOME_CLASSES,
        )
    if candidate_class and (not allowed_outcomes or candidate_class in allowed_outcomes):
        return candidate_outcome
    fallback_outcome = str((constraints or {}).get("default_outcome") or OUTCOME_REPORT_ONLY).strip().lower()
    fallback_reason = str(
        (constraints or {}).get("default_reason")
        or (constraints or {}).get("reason")
        or "semantic_adjudication_required"
    )
    return build_execution_outcome(
        fallback_outcome,
        reason=fallback_reason,
        source=OUTCOME_SOURCE_RULE,
        productive=fallback_outcome in PRODUCTIVE_OUTCOME_CLASSES,
    )


def maybe_adjudicate_outcome_with_model(
    *,
    task_type: str,
    execution_facts: Dict[str, Any],
    final_text: str,
    deterministic_outcome: Dict[str, Any],
) -> Dict[str, Any]:
    """Use a small model only for ambiguous semantic cases."""
    constraints = derive_outcome_constraints_from_facts(
        task_type=task_type,
        execution_facts=execution_facts,
        final_text=final_text,
    )
    if not bool(constraints.get("semantic_adjudication")):
        return deterministic_outcome

    if str(os.environ.get("OUROBOROS_ENABLE_OUTCOME_ADJUDICATION", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
        return deterministic_outcome

    facts = execution_facts if isinstance(execution_facts, dict) else default_execution_facts()
    allowed_outcomes = sorted(
        str(item).strip().lower()
        for item in ((constraints or {}).get("allowed_outcomes") or set())
        if str(item).strip()
    )
    prompt = (
        "Classify this Ouroboros task completion.\n"
        "Return strict JSON with keys outcome_class, outcome_reason, confidence.\n"
        "Choose ONLY from the allowed_outcomes list. Use semantic judgment, not just tool counts.\n"
        "Treat cosmetic bookkeeping and owner-directed follow-up as non-productive.\n\n"
        f"task_type={str(task_type or '').strip().lower() or 'task'}\n"
        f"allowed_outcomes={json.dumps(allowed_outcomes, ensure_ascii=False)}\n"
        f"trigger_reason={json.dumps(str((constraints or {}).get('reason') or ''), ensure_ascii=False)}\n"
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
        confidence = payload.get("confidence")
        if candidate not in set(allowed_outcomes):
            return resolve_outcome_conflict(
                constraints=constraints,
                candidate_outcome=deterministic_outcome,
            )
        resolved = build_execution_outcome(
            candidate,
            reason=reason or "model_adjudicated_ambiguous_completion",
            source=OUTCOME_SOURCE_MODEL,
            confidence=confidence if isinstance(confidence, (int, float, str)) else None,
        )
        try:
            conf_value = float(resolved.get("confidence"))
        except Exception:
            conf_value = 1.0
        if conf_value < 0.7:
            return resolve_outcome_conflict(
                constraints=constraints,
                candidate_outcome=deterministic_outcome,
            )
        return resolve_outcome_conflict(
            constraints=constraints,
            candidate_outcome=resolved,
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
    mutating_tools = _normalized_mutating_tools(facts)

    if normalized_task_type == "evolution":
        if current == OUTCOME_REPORT_ONLY:
            return build_execution_outcome(
                OUTCOME_FAILED,
                reason="evolution_requires_concrete_work_not_report_only",
                source=outcome.get("outcome_source") or OUTCOME_SOURCE_RULE,
                productive=False,
            )
        if current == OUTCOME_EXECUTED_WORK and mutating_tools == {"knowledge_write"}:
            return build_execution_outcome(
                OUTCOME_FAILED,
                reason="evolution_knowledge_only_write_not_sufficient",
                source=outcome.get("outcome_source") or OUTCOME_SOURCE_RULE,
                productive=False,
            )
        if current == OUTCOME_EXECUTED_WORK and int(facts.get("repo_commit_calls") or 0) <= 0:
            return build_execution_outcome(
                OUTCOME_FAILED,
                reason="evolution_requires_commit_for_success",
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
