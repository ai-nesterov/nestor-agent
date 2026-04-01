# Executor Selection Policy v1

## Routing rubric

Default executor choices:
- `ouroboros`: default path for straightforward work the main agent can handle comfortably itself
- `claude_code`: strong colleague for planning, review, or architecture-heavy multi-file refactors
- `codex`: strong colleague for planning, review, or deterministic implementation-heavy tasks

Core rule:
- external executors are escalation paths, not the default
- use them when task complexity exceeds the main agent's easy working envelope
- if the main agent is getting stuck, looping, or expects the task to need a stronger pass, ask an external executor for help

## Restricted callers

By default external executors are disallowed for:
- background consciousness
- evolution (unless explicitly enabled by flag)

Review is an allowed use case when enabled by policy/config because external
workers are explicitly intended to help with harder review passes.

## Import workflow reminder

External execution does not imply acceptance.
Always follow:
1. validate executor artifacts
2. explicit patch import
3. existing review gate before commit
