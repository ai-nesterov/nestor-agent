# External Executor Policy v2 (Planning + Review + Coding)

## Scope split

- `ouroboros`: dialogue, reasoning, self-reflection, final coordination.
- external workers (`claude_code`, `codex`): planning tasks, review tasks, implementation tasks when the job has become harder than the main agent should handle alone in one comfortable pass.

Working model:
- the main agent remains primary and should solve straightforward work itself
- external workers are the escalation path when the task is too complex, the agent is getting stuck, or a stronger specialist pass is warranted
- use them the way you would ask a more experienced colleague for help

## Task metadata

Scheduler now supports:
- `task_type`: queue class (`task|review|evolution`)
- `task_kind`: semantic class (`general|review_plan|review_code|refactor_plan|evolution_plan|evolution_implement|evolution_verify|implement`)
- `caller_class`: policy caller id (`main_task_agent|review|consciousness|...`)
- `model_policy`: `cheap|balanced|premium|critical`
- `model_override`: optional explicit model pin
- `importance`: `low|medium|high|critical`
- `defer_on_quota`: defer important blocked tasks instead of rejecting
- `budget_decision`: `auto|defer|force_run` (agent-side budget decision)

## Budget-saving modes

Derived from daily-cap consumption:
- `normal` (<70%)
- `conserve` (>=70%)
- `critical` (>=90%)

In `conserve/critical`, premium/critical external runs can be blocked and deferred.
`force_run` can bypass this soft-budget block (hard caps and capacity still apply).
`defer` explicitly pushes the task into deferred queue even when capacity exists.

## Deferred queue

High-importance external tasks blocked by quota/policy can be stored in `state.deferred_tasks`.
Use `resume_deferred_tasks(limit=...)` to re-admit when capacity is available.

## Caller restrictions

- consciousness/external remains controlled by dedicated flags.
- review caller can use external workers when `*_ALLOWED_IN_REVIEW=true`.
- evolution usage remains gated by `*_ALLOWED_IN_EVOLUTION`.
