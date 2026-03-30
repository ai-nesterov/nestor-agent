# Executor Selection Policy v1

## Routing rubric

Default executor choices:
- `ouroboros`: planning, analysis, reviews, small tasks
- `claude_code`: architecture-heavy multi-file refactors
- `codex`: deterministic implementation-heavy tasks

## Restricted callers

By default external executors are disallowed for:
- review caller class
- background consciousness
- evolution (unless explicitly enabled by flag)

## Import workflow reminder

External execution does not imply acceptance.
Always follow:
1. validate executor artifacts
2. explicit patch import
3. existing review gate before commit

