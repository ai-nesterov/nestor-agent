# Claude Code Worker v1

Status: v1 executor contract.

## Runtime mode

Claude worker is executed via CLI print mode in non-interactive flow.

Baseline command shape:
- `claude -p <prompt> --output-format json --model <model>`

## Auth policy

Supported modes:
- `subscription_only`
- `api_only`
- `auto`

v1 enforcement:
- `subscription_only` fails if `ANTHROPIC_API_KEY` exists in environment.
- `api_only` fails if `ANTHROPIC_API_KEY` is missing.
- Auth mode is recorded in result usage metadata.

## Safety policy

- All execution happens inside per-task isolated worktree.
- Protected paths (`BIBLE.md`, safety-critical files) are rolled back post-run.
- If protected paths were touched, task result becomes `failed`.

## Result policy

- Runner writes `stdout.txt`, `stderr.txt`, `events.jsonl`.
- Result payload is parsed from JSON output when available.
- Empty-diff completion requires explicit `no-change` reason, otherwise failed by stop-policy guard.

