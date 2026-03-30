# Executor Routing and Quotas v1

## Admission control

External executor tasks are accepted only when:
- `EXTERNAL_EXECUTORS_ENABLED=true`
- per-executor enable flag is true (`CLAUDE_CODE_ENABLED` / `CODEX_ENABLED`)
- context policy allows caller/task type
- current active runs are below per-executor parallel cap
- daily runs are below per-executor daily cap

Admission is enforced server-side during schedule event handling.

## State counters

Persistent state fields:
- `codex_runs_today`
- `claude_code_runs_today`
- `codex_active`
- `claude_code_active`
- `last_reset_at`

Counters reset on UTC date change.

## Default safety posture

- external executors disabled by default
- evolution access disabled by default
- conservative parallel and daily limits

