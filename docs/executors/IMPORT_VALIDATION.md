# Executor Result Validation and Import v1

## Validation entrypoint

Use `validate_executor_result(task_id)` before import.

Checks:
- required artifact files exist
- manifest validity and `base_sha`
- non-empty `patch.diff`
- patch does not touch protected paths
- `result.json` is valid JSON object

## Import entrypoint

Use `apply_task_patch(task_id)` only after successful validation.

Import gates:
- main repo must be clean
- `base_sha` must match current `HEAD`
- patch apply must succeed without auto-commit

## Rejection policy

Import is rejected for:
- missing/invalid artifacts
- empty patch
- protected path edits
- stale base SHA
- dirty main repo

