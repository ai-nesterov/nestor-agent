# External Executor Policy v2 (Planning + Review + Coding)

## Scope split

- `ouroboros`: dialogue, reasoning, self-reflection, final coordination.
- external workers (`claude_code`, `codex`): planning tasks, review tasks, implementation tasks.

## Task metadata

Scheduler now supports:
- `task_type`: queue class (`task|review|evolution`)
- `task_kind`: semantic class (`general|review_plan|review_code|refactor_plan|evolution_plan|implement`)
- `caller_class`: policy caller id (`main_task_agent|review|consciousness|...`)
- `model_policy`: `cheap|balanced|premium|critical`
- `model_override`: optional explicit model pin
- `importance`: `low|medium|high|critical`
- `defer_on_quota`: defer important blocked tasks instead of rejecting

## Budget-saving modes

Derived from daily-cap consumption:
- `normal` (<70%)
- `conserve` (>=70%)
- `critical` (>=90%)

In `conserve/critical`, premium/critical external runs can be blocked and deferred.

## Deferred queue

High-importance external tasks blocked by quota/policy can be stored in `state.deferred_tasks`.
Use `resume_deferred_tasks(limit=...)` to re-admit when capacity is available.

## Caller restrictions

- consciousness/external remains controlled by dedicated flags.
- review caller can use external workers when `*_ALLOWED_IN_REVIEW=true`.
- evolution usage remains gated by `*_ALLOWED_IN_EVOLUTION`.

