# Executor Result Schema v1

Status: normative for all executor task terminal results.

## JSON shape

```json
{
  "task_id": "string",
  "executor": "claude_code|codex|ouroboros",
  "status": "completed|failed|timeout|cancelled",
  "summary": "string",
  "result_text": "string",
  "artifact_dir": "string",
  "patch_path": "string|null",
  "stdout_path": "string|null",
  "stderr_path": "string|null",
  "jsonl_path": "string|null",
  "changed_files": ["..."],
  "diff_stat": {
    "files": 0,
    "insertions": 0,
    "deletions": 0
  },
  "usage": {
    "auth_mode": "subscription|api|unknown",
    "usage_kind": "subscription_quota|api_cost|unknown",
    "cost_usd": null,
    "model": "string|null"
  },
  "base_sha": "string",
  "worktree_path": "string|null",
  "worktree_retained": false,
  "external_session_id": "string|null",
  "timings": {
    "started_at": "iso8601",
    "finished_at": "iso8601",
    "duration_sec": 0
  },
  "executor_result_schema_version": 1
}
```

## Validation requirements

A terminal external result is valid only if:
- Schema validation passes.
- Required artifact files exist (or nullable fields are explicitly null).
- `base_sha` is present.
- `status` is terminal.
- `usage.auth_mode` is set and policy-compatible.

## Import preconditions

`apply_task_patch(task_id)` must fail unless:
- Result schema validates.
- Manifest exists and matches task/result identity.
- Main repo is clean.
- Current HEAD matches/descends from result `base_sha` policy.
- Protected paths are unchanged by patch.

Import operation must not create commit.

## Legacy compatibility

`get_task_result` must tolerate old result files missing schema/usage fields.
Default unknown values:
- `usage.auth_mode = "unknown"`
- `usage.usage_kind = "unknown"`
- `executor_result_schema_version = 0`

