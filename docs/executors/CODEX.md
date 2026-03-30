# Codex Worker v1

Status: v1 executor contract.

## Runtime mode

Codex worker runs via non-interactive CLI:
- `codex exec <prompt> --json --output-schema <schema.json> -o <result.json>`

## Project-scoped config

Each isolated worktree receives `.codex/config.toml` with pinned defaults:
- `model = "gpt-5.4"`
- `approval_policy = "on-request"`
- `sandbox_mode = "workspace-write"`

## Auth policy

Supported modes:
- `subscription_only`
- `api_only`
- `auto`

v1 enforcement:
- `subscription_only` fails if `CODEX_API_KEY` is present.
- `api_only` fails if `CODEX_API_KEY` is missing.
- Silent auth fallback is not allowed.

## Structured output

Final output is validated against required schema fields:
- `summary`
- `tests_run`
- `tests_passed`
- `changed_files`
- `risk_summary`
- `made_changes`

Schema mismatch marks task as failed.

## Safety

- Protected paths are rolled back post-run.
- Any protected file mutation marks task as failed.

