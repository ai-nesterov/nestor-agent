# Executor Evals v1

Deterministic eval scenarios are mapped to dedicated pytest tests.

Core scenarios covered:
1. schedule Claude task
2. schedule Codex task
3. quota exceeded
4. auth mismatch
5. stale base SHA
6. dirty main repo
7. patch import success
8. patch import reject
9. protected path attack
10. timeout cleanup (runner timeout path)
11. interrupted worker (covered by existing worker interruption tests)
12. no-change task policy

Use `python scripts/run_executor_evals.py` to run the staged suite.
