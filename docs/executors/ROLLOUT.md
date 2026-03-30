# External Executors Rollout Plan

Status: staged rollout contract for v1.

## Feature flags

Global:
- `EXTERNAL_EXECUTORS_ENABLED`

Per executor:
- `CLAUDE_CODE_ENABLED`
- `CODEX_ENABLED`

Auth mode:
- `CLAUDE_CODE_AUTH_MODE = subscription_only|api_only|auto`
- `CODEX_AUTH_MODE = subscription_only|api_only|auto`

Concurrency and quotas:
- `CLAUDE_CODE_MAX_PARALLEL`
- `CODEX_MAX_PARALLEL`
- `CLAUDE_CODE_DAILY_TASK_CAP`
- `CODEX_DAILY_TASK_CAP`

Context restrictions:
- `CLAUDE_ALLOWED_IN_EVOLUTION`
- `CODEX_ALLOWED_IN_EVOLUTION`
- `CLAUDE_ALLOWED_IN_CONSCIOUSNESS`
- `CODEX_ALLOWED_IN_CONSCIOUSNESS`

## Default safe policy (v1)

- External executors disabled by default.
- Consciousness and evolution cannot call external executors.
- Max parallel for each external executor: 1.
- Daily caps low.
- Patch import always explicit.

## Stages

1. Phase 1: feature complete, disabled by default.
2. Phase 2: Claude enabled in dev only.
3. Phase 3: Codex enabled in dev only.
4. Phase 4: both enabled for manual user-invoked subtasks.
5. Phase 5: optional smart routing by policy.
6. Phase 6: optional limited evolution usage in separate PR.

## Observability and audit

Must log per run:
- task id, executor, caller class
- auth mode and usage kind
- timing (start/end/duration)
- status and failure reason
- changed files and diff stat
- patch import status

## Rollback

Immediate rollback procedure:
1. Set `EXTERNAL_EXECUTORS_ENABLED=false`.
2. Drain/finish in-flight external tasks.
3. Disable per-executor flags.
4. Preserve artifacts for audit.

No schema rollback is required because v1 additions are backward-compatible and additive.

