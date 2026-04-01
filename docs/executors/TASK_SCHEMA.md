# Task Schema v2

Status: normative for external executor scheduling.

## JSON shape

```json
{
  "id": "string",
  "type": "task|review|evolution",
  "executor": "ouroboros|claude_code|codex",
  "executor_mode": "internal_agent|external_cli",
  "description": "string",
  "context": "string",
  "chat_id": "string|null",
  "parent_task_id": "string|null",
  "depth": 0,
  "priority": 0,
  "repo_scope": ["path/a", "path/b"],
  "artifact_policy": "patch_only|keep_worktree",
  "quota_class": "cheap|expensive",
  "constraints": {
    "allow_network": false,
    "allow_long_run": false,
    "require_tests": true,
    "require_structured_output": true
  },
  "created_at": "iso8601",
  "task_schema_version": 2
}
```

## Defaults (for backward compatibility)

If fields are absent:
- `executor = "ouroboros"`
- `executor_mode = "internal_agent"`
- `repo_scope = []`
- `artifact_policy = "patch_only"`
- `quota_class = "cheap"`
- `constraints.allow_network = false`
- `constraints.allow_long_run = false`
- `constraints.require_tests = true`
- `constraints.require_structured_output = false` for legacy tasks unless explicitly set
- `task_schema_version = 1` for pre-v2 tasks

## Scheduling and dedup keys

Duplicate detection must include:
- `type`
- `executor`
- `parent_task_id`
- normalized `description`

Tasks with same description but different executor are not automatically duplicates.

## Caller policy hints

v1 policy defaults:
- Main task agent: external executor allowed.
- Review tasks: external executor allowed when policy/config enables review-side escalation.
- Consciousness: disallowed.
- Evolution: disallowed by default.
