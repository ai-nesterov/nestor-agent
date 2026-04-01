"""Task-start tool visibility policy.

This module is the source of truth for which tools are available at the start
of a task without an explicit `enable_tools` call.

Keep this separate from `ouroboros.tools.registry`: the registry owns tool
dispatch and the safety sandbox, while this module owns everyday visibility
policy. That split lets Ouroboros tune the default toolset without editing a
protected safety-critical file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol


CORE_TOOL_NAMES = frozenset({
    "repo_read", "repo_list", "repo_write", "repo_write_commit", "repo_commit", "str_replace_editor",
    "data_read", "data_list", "data_write",
    "run_shell", "claude_code_edit",
    "git_status", "git_diff",
    "restore_to_head", "revert_commit",
    "pull_from_remote",
    "schedule_task", "wait_for_task", "get_task_result", "resume_deferred_tasks",
    "validate_executor_result", "apply_task_patch", "discard_task_patch",
    "update_scratchpad", "update_identity",
    "chat_history", "web_search",
    "send_user_message", "send_photo", "switch_model",
    "request_restart", "promote_to_stable",
    "knowledge_read", "knowledge_write", "knowledge_list",
    "browse_page", "browser_action", "analyze_screenshot",
})

META_TOOL_NAMES = frozenset({"list_available_tools", "enable_tools"})


def recommend_executor(
    *,
    task_type: str = "task",
    analysis_only: bool = False,
    implementation_heavy: bool = False,
    architecture_heavy: bool = False,
    deterministic_output_required: bool = False,
) -> str:
    """Heuristic recommendation for schedule_task(executor=...).

    External executors are escalation paths, not the default.

    Working rubric:
    - ouroboros: straightforward work the main agent can handle comfortably
    - claude_code: stronger colleague for harder planning/review passes and
      architecture-heavy multi-file refactors
    - codex: stronger colleague for harder planning/review passes and
      deterministic implementation-heavy work
    """

    tt = str(task_type or "task").strip().lower()
    if tt == "consciousness":
        return "ouroboros"
    if analysis_only:
        return "ouroboros"
    if tt == "review":
        if architecture_heavy:
            return "claude_code"
        if deterministic_output_required or implementation_heavy:
            return "codex"
        return "ouroboros"
    if architecture_heavy:
        return "claude_code"
    if implementation_heavy and deterministic_output_required:
        return "codex"
    return "ouroboros"


def caller_can_schedule_external_executor(
    *,
    caller_class: str,
    task_type: str = "task",
    allow_evolution: bool = False,
    allow_consciousness: bool = False,
) -> bool:
    """Return whether external executors are allowed for this caller/task class."""

    cc = str(caller_class or "").strip().lower()
    tt = str(task_type or "task").strip().lower()
    if cc == "consciousness":
        return bool(allow_consciousness)
    if cc == "review":
        return False
    if tt == "evolution":
        return bool(allow_evolution)
    return cc in {"main_task_agent", "human_invoked", "task_agent", ""}


class ToolSchemaProvider(Protocol):
    """Minimal registry contract needed by the loop/discovery helpers."""

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        ...


def is_initial_task_tool(name: str) -> bool:
    """Return True if the tool should be loaded before any enable_tools call."""

    return name in CORE_TOOL_NAMES or name in META_TOOL_NAMES


def initial_tool_schemas(registry: ToolSchemaProvider) -> List[Dict[str, Any]]:
    """Return the schemas that should be present from round 1."""

    result = []
    for schema in registry.schemas():
        name = schema.get("function", {}).get("name", "")
        if is_initial_task_tool(name):
            result.append(schema)
    return result


def list_non_core_tools(registry: ToolSchemaProvider) -> List[Dict[str, str]]:
    """Return name+description for tools that require explicit enable_tools."""

    result = []
    for schema in registry.schemas():
        function = schema.get("function", {})
        name = function.get("name", "")
        if not name or is_initial_task_tool(name):
            continue
        result.append({
            "name": name,
            "description": function.get("description", "No description"),
        })
    return result
