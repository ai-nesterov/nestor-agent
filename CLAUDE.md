# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server (then open http://127.0.0.1:8765)
python server.py

# Run all tests
make test
# or
python3 -m pytest tests/ -q --tb=short

# Run a single test file
python3 -m pytest tests/test_smoke.py -v

# Run a single test by name
python3 -m pytest tests/test_smoke.py::test_function_name -v

# Codebase health check (module complexity metrics)
make health

# Lint (ruff, line-length 120)
ruff check .
```

## Architecture

This is **Ouroboros** — a self-modifying AI agent that writes and commits its own code. The agent runs inside a Starlette/uvicorn server and exposes a web UI.

### Two-process model

- **`launcher.py`** — Immutable outer shell (PyWebView desktop window). Manages the PID lock, bootstraps `~/Ouroboros/repo/` on first run, syncs safety-critical files on every launch (`prompts/SAFETY.md`, `ouroboros/safety.py`, `ouroboros/tools/registry.py`), spawns `server.py`. Never self-modifies.
- **`server.py` + `nestor/`** — Self-editable inner server (HTTP + WebSocket on port 8765):
  - `nestor/http.py` — HTTP handlers and route assembly
  - `nestor/websocket.py` — WebSocket client lifecycle and broadcast
  - `nestor/state.py` — Shared runtime state, supervisor startup, slash commands, panic/restart
- **`telegram_bot.py`** — Optional standalone aiogram 3.x process (polling mode). Communicates with `server.py` via `/api/telegram/process-message`.

### Supervisor (`supervisor/`)

Background thread (started by `nestor/state.py`) that manages the task queue, worker pool, evolution loop, and message routing. Key files: `workers.py` (multiprocessing pool), `queue.py` (PENDING/RUNNING), `message_bus.py` (LocalChatBridge), `git_ops.py` (clone/push/rollback), `state.py` (persistent `state.json`).

### Agent core (`ouroboros/`)

Runs inside worker processes spawned by the supervisor.

- **`config.py`** — SSOT for all paths, settings defaults, load/save. Settings live in `~/Ouroboros/data/settings.json`. Runtime paths: `APP_ROOT = ~/Ouroboros/`.
- **`agent.py`** — Top-level task orchestrator. Calls `agent_startup_checks.py` then `agent_task_pipeline.py`.
- **`loop.py` / `loop_llm_call.py` / `loop_tool_execution.py`** — LLM tool loop split across three files: high-level coordination, single-round LLM call + usage accounting, and tool dispatch.
- **`context.py`** — Builds the full LLM context (system prompt + BIBLE + identity + scratchpad + health invariants + memory registry + recent events). Health invariants are placed first in the dynamic block.
- **`llm.py`** — Routes LLM calls to cloud (OpenRouter/MiniMax) or local (llama-cpp-python) based on per-lane settings (`USE_LOCAL_MAIN`, `USE_LOCAL_CODE`, etc.).
- **`safety.py` + `tools/registry.py`** — Four-layer safety: hardcoded sandbox (blocks writes to critical files and mutative git via shell) → deterministic whitelist → LLM fast check → LLM deep check.
- **`memory.py`** — Scratchpad (`scratchpad_blocks.json`), identity (`identity.md`), and chat history.
- **`consolidator.py`** — Block-wise dialogue consolidation (`dialogue_blocks.json`, max 10 blocks with era compression) and scratchpad auto-consolidation (triggered at >30k chars).
- **`consciousness.py`** — Background thinking daemon. Pauses when tasks run. Caps at 10% of total budget.
- **`reflection.py`** — Post-task execution reflection stored in `task_reflections.jsonl`.
- **`review.py`** — Full-codebase review pipeline and complexity metrics.
- **`pricing.py`** — Model pricing table and per-call cost accounting.
- **`tools/`** — Auto-discovered tool plugins. Tools are named `{verb}_{noun}` and exported from `get_tools()` in each module.
- **`executors/`** — External executor workers (Claude Code CLI, Codex CLI) for delegating complex tasks to isolated specialist processes.

### Web UI (`web/`)

Single-page app. `web/app.js` is a thin orchestrator (~90 lines) that imports from `web/modules/` (chat, dashboard, logs, evolution, settings, costs, versions, about, ws, utils). Chart.js is bundled locally (`web/chart.umd.min.js`).

### Data layout

All runtime data lives in `~/Ouroboros/data/`. The agent's self-modifying git repo lives in `~/Ouroboros/repo/`.

## Key invariants

1. **VERSION == `pyproject.toml` version == latest git tag == README badge == `docs/ARCHITECTURE.md` header version.** Discrepancy is a bug — fix immediately.
2. **`docs/ARCHITECTURE.md` is the single source of truth** for components, API endpoints, and data flows. Update it in the same commit as any structural change.
3. **`docs/CHECKLISTS.md` is the single source of truth** for pre-commit review criteria. Add new review concerns there, not in prompts or docs.
4. **Every commit through `repo_commit` runs multi-model pre-commit review** against `docs/CHECKLISTS.md`. Review enforcement is `advisory` (default) or `blocking`.
5. **Module size budget: ~1000 lines max per module, <150 lines per method, <8 function parameters** (BIBLE P5 Minimalism).
6. **`ouroboros/config.py` is SSOT** for all settings defaults and paths. Never hardcode paths elsewhere.
7. **New tools must be exported from `get_tools()`** in their module and registered in `tool_policy.py`.
8. **`BIBLE.md` and `identity.md` must never be deleted.** `identity.md` content is intentionally mutable (agent self-creation). `BIBLE.md` content and git history are absolutely protected.
9. **Preferred multi-file workflow:** `repo_write` all files first, then a single `repo_commit` to stage, review, and commit everything as one diff.

## Branching model

- `ouroboros` — development branch; agent commits here
- `ouroboros-stable` — promoted stable version
- `main` — protected; agent never touches it

## Testing notes

Tests in `tests/` are designed to run without external APIs or running servers. Use `tests/stubs/fake_claude.py` and `tests/stubs/fake_codex.py` for executor stubs. The eval scenarios are in `tests/evals/` and are run separately via `scripts/run_executor_evals.py`.
