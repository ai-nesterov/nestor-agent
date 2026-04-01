# External Executors Architecture (v1)

Status: frozen contract for PR-00..PR-08

This document defines mandatory contracts for integrating external coding executors (`claude_code`, `codex`) into Ouroboros subtask execution.

## Scope and goals

v1 adds executor-aware subtask routing with isolated execution and explicit patch import.

Target outcome:
- Main Ouroboros agent remains the only orchestrator.
- External executors act as stronger specialist colleagues when the task is beyond
  what the main agent can solve comfortably on its own.
- External executors run only as isolated subtask workers.
- Each external run executes in isolated git worktree.
- External run output is structured artifacts + schema-validated result.
- Patch import is explicit (`apply_task_patch`), separate from run.
- Existing review and commit gates stay authoritative.

Primary intended use cases:
- planning tasks
- review tasks
- implementation tasks

Selection is an escalation decision, not a default. If the main agent can solve
the problem directly without strain, it should do so. If it gets stuck, expects
the task to exceed its comfortable complexity envelope, or wants a stronger pass,
it should ask an external executor for help.

## Non-negotiable invariants

1. External executors never write directly to main `REPO_DIR`.
2. External executors never commit/push to main repo.
3. Every external task runs in isolated worktree.
4. Every external task returns schema-validated structured result.
5. Patch import is a separate explicit operation.
6. Background consciousness cannot invoke external executors in v1.
7. Evolution cannot invoke external executors in v1 without feature flag.
8. Auth mode must be explicit (`subscription` vs `api`) and separately logged.
9. Worker timeout must terminate child process and cleanup worktree.
10. Protected path modifications must be blocked or rolled back post-run.

## Layering and responsibilities

- `schedule_task` + queue/state: admission and routing intent.
- Worker pools: execution capability (`ouroboros`, `claude_code`, `codex`).
- Executor runtime layer: process launch, worktree lifecycle, artifact capture.
- Result validation/import layer: manifest/schema validation + guarded patch apply.
- Review gate: final acceptance and commit decision.

External executors are opaque worker backends and must not be treated as drop-in `LLMClient` models.
They are isolated helpers that the main agent consults when it needs a stronger
planning, review, or implementation pass.

## Execution flow

1. Agent calls `schedule_task(..., executor=...)`.
2. Event pipeline persists task with executor metadata.
3. Queue admission checks policy, quota, and pool capacity.
4. Worker creates isolated worktree from allowed branch (`branch_dev` only).
5. Runner executes external CLI in noninteractive mode.
6. Runtime writes structured artifacts and `ExecutorResult`.
7. Task result pipeline exposes run summary and import hint.
8. Optional explicit `apply_task_patch(task_id)` imports patch into main repo.
9. Existing review toolchain decides whether commit is allowed.

## Worktree contract

Worktree branch naming:
- `task/<task_id>/<executor>`

Required behavior:
- Record `base_sha` at worktree creation.
- Never operate from `stable`.
- Cleanup in all terminal paths (success/failure/timeout/cancel).
- Optional retention only via explicit artifact policy (`keep_worktree`).

## Artifact contract

Per-run artifact root:
- `data/executor_runs/<task_id>/`

Required files (nullable only where documented in result schema):
- `manifest.json`
- `prompt.txt`
- `result.json`
- `stdout.txt`
- `stderr.txt`
- `events.jsonl`
- `patch.diff`
- `changed_files.json`
- `diff_stat.json`

## Auth and billing contract

Auth mode is explicit, policy-checked before launch, and logged in both manifest and result.

- `subscription_only`: fail fast if runtime environment indicates API mode fallback.
- `api_only`: fail fast if required API credentials are absent.
- Silent fallback between auth modes is prohibited.

## Failure and cleanup guarantees

Timeout/crash path requirements:
- Process termination uses process group kill semantics.
- Cleanup runs in `finally` and is idempotent.
- Worktree removal executes even if runner fails post-processing.
- Result status must be terminal (`failed|timeout|cancelled`) with reason.

## Migration and compatibility

Task/result schema versions are additive in v1.

Compatibility rules:
- Existing tasks/results without new fields remain readable.
- Queue snapshot restore tolerates missing executor fields and defaults to `ouroboros`.
- New fields must have deterministic defaults.
- Parser must ignore unknown future fields.

Versioning:
- Task schema: `task_schema_version = 2`
- Result schema: `executor_result_schema_version = 1`

## Out of scope for v1

- Auto-import + auto-commit path.
- External executor invocation from consciousness.
- External executor invocation from evolution by default.
- Codex cloud tasks integration.
