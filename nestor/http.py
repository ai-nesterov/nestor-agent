"""HTTP handlers and route assembly for the server."""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from ouroboros.config import get_cloud_provider, read_version as _read_version
from ouroboros.local_model_api import (
    api_local_model_start,
    api_local_model_status,
    api_local_model_stop,
    api_local_model_test,
)

from nestor import state, websocket


async def api_health(request: Request) -> JSONResponse:
    runtime_version = _read_version()
    app_version = os.environ.get("OUROBOROS_APP_VERSION", "").strip() or runtime_version
    return JSONResponse(
        {
            "status": "ok",
            "version": runtime_version,
            "runtime_version": runtime_version,
            "app_version": app_version,
        }
    )


async def api_state(request: Request) -> JSONResponse:
    try:
        from supervisor.state import TOTAL_BUDGET_LIMIT, load_state
        from supervisor.workers import PENDING, RUNNING, WORKERS

        st = load_state()
        alive = 0
        total_w = 0
        try:
            alive = sum(1 for w in WORKERS.values() if w.proc.is_alive())
            total_w = len(WORKERS)
        except Exception:
            pass
        spent = float(st.get("spent_usd") or 0.0)
        limit = float(TOTAL_BUDGET_LIMIT or 10.0)
        return JSONResponse(
            {
                "uptime": int(time.time() - state.APP_START),
                "workers_alive": alive,
                "workers_total": total_w,
                "pending_count": len(PENDING),
                "running_count": len(RUNNING),
                "spent_usd": round(spent, 4),
                "budget_limit": limit,
                "budget_pct": round((spent / limit * 100) if limit > 0 else 0, 1),
                "branch": st.get("current_branch", "ouroboros"),
                "sha": (st.get("current_sha") or "")[:8],
                "evolution_enabled": bool(st.get("evolution_mode_enabled")),
                "bg_consciousness_enabled": bool(st.get("bg_consciousness_enabled")),
                "evolution_cycle": int(st.get("evolution_cycle") or 0),
                "spent_calls": int(st.get("spent_calls") or 0),
                "minimax_requests_5h_used": int(st.get("minimax_requests_5h_used") or 0),
                "minimax_requests_5h_limit": int(st.get("minimax_requests_5h_limit") or 0),
                "minimax_requests_5h_remaining": st.get("minimax_requests_5h_remaining"),
                "minimax_requests_weekly_used": int(st.get("minimax_requests_weekly_used") or 0),
                "minimax_requests_weekly_limit": int(st.get("minimax_requests_weekly_limit") or 0),
                "minimax_requests_weekly_remaining": st.get("minimax_requests_weekly_remaining"),
                "supervisor_ready": state.supervisor_is_ready(),
                "supervisor_error": state.get_supervisor_error(),
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_executor_status(request: Request) -> JSONResponse:
    def _run_status(cmd: list[str], timeout_sec: float = 8.0) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(state.REPO_DIR),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            merged = "\n".join([p for p in (out, err) if p]).strip()
            if proc.returncode != 0:
                return False, merged or f"exit={proc.returncode}"
            return True, merged
        except Exception as exc:
            return False, str(exc)

    def _probe_claude_usage_limits() -> dict:
        ok, raw = _run_status(["claude", "-p", "/usage", "--output-format", "text"], timeout_sec=10.0)
        text = (raw or "").strip()
        lowered = text.lower()
        if not ok:
            return {
                "five_hour_remaining": None,
                "weekly_remaining": None,
                "source": "probe_failed",
                "note": f"Claude /usage probe failed: {text[:140] or 'no output'}",
            }
        if "unknown skill: usage" in lowered:
            return {
                "five_hour_remaining": None,
                "weekly_remaining": None,
                "source": "slash_unavailable_noninteractive",
                "note": "Claude CLI /usage is unavailable in non-interactive print mode.",
            }
        if not text:
            return {
                "five_hour_remaining": None,
                "weekly_remaining": None,
                "source": "empty_probe_output",
                "note": "Claude /usage returned empty output in non-interactive mode.",
            }

        five_hour_remaining = None
        weekly_remaining = None
        five_match = re.search(
            r"(?:5\s*[- ]?hour|5h)[^\n]{0,80}?remaining[^\n:]*[:=]\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        week_match = re.search(
            r"(?:weekly|week)[^\n]{0,80}?remaining[^\n:]*[:=]\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        if five_match:
            five_hour_remaining = five_match.group(1).strip()
        if week_match:
            weekly_remaining = week_match.group(1).strip()

        if five_hour_remaining is None and weekly_remaining is None:
            return {
                "five_hour_remaining": None,
                "weekly_remaining": None,
                "source": "unparseable_probe_output",
                "note": "Claude /usage responded, but no parseable 5h/week remaining values were found.",
            }
        return {
            "five_hour_remaining": five_hour_remaining,
            "weekly_remaining": weekly_remaining,
            "source": "claude_usage_probe",
            "note": "",
        }

    try:
        from supervisor.state import load_state

        st = load_state()
        settings = state.load_settings()

        codex_ok, codex_raw = _run_status(["codex", "login", "status"])
        claude_ok, claude_raw = _run_status(["claude", "auth", "status"])

        claude_json = {}
        if claude_ok:
            try:
                claude_json = json.loads(claude_raw)
            except Exception:
                claude_json = {}

        codex_logged_in = codex_ok and ("logged in" in codex_raw.lower())
        codex_auth_method = "unknown"
        if codex_logged_in:
            low = codex_raw.lower()
            if "chatgpt" in low:
                codex_auth_method = "chatgpt"
            elif "api" in low:
                codex_auth_method = "api"

        codex_cap = int(settings.get("CODEX_DAILY_TASK_CAP", 5) or 5)
        claude_cap = int(settings.get("CLAUDE_CODE_DAILY_TASK_CAP", 5) or 5)
        codex_used = int(st.get("codex_runs_today") or 0)
        claude_used = int(st.get("claude_code_runs_today") or 0)
        claude_usage_limits = _probe_claude_usage_limits()
        codex_limits = {
            "five_hour_remaining": None,
            "weekly_remaining": None,
            "source": "not_exposed_by_cli",
            "note": "Codex CLI does not expose subscription 5h/week remaining quotas via a stable non-interactive command.",
        }
        note_parts = [
            str(codex_limits.get("note") or "").strip(),
            str(claude_usage_limits.get("note") or "").strip(),
        ]
        global_note = " ".join([p for p in note_parts if p]).strip()

        return JSONResponse(
            {
                "external_budget_mode": str(st.get("external_budget_mode") or "normal"),
                "deferred_tasks_count": len(st.get("deferred_tasks") or []),
                "codex": {
                    "status_ok": codex_ok,
                    "raw_status": codex_raw,
                    "logged_in": codex_logged_in,
                    "auth_method": codex_auth_method,
                    "daily_cap": codex_cap,
                    "daily_used": codex_used,
                    "daily_remaining": max(0, codex_cap - codex_used),
                    "five_hour_remaining": codex_limits.get("five_hour_remaining"),
                    "weekly_remaining": codex_limits.get("weekly_remaining"),
                    "limits_source": codex_limits.get("source"),
                },
                "claude": {
                    "status_ok": claude_ok,
                    "raw_status": claude_raw,
                    "logged_in": bool(claude_json.get("loggedIn")) if claude_json else claude_ok,
                    "auth_method": str(claude_json.get("authMethod") or "unknown"),
                    "subscription_type": str(claude_json.get("subscriptionType") or "unknown"),
                    "daily_cap": claude_cap,
                    "daily_used": claude_used,
                    "daily_remaining": max(0, claude_cap - claude_used),
                    "five_hour_remaining": claude_usage_limits.get("five_hour_remaining"),
                    "weekly_remaining": claude_usage_limits.get("weekly_remaining"),
                    "limits_source": claude_usage_limits.get("source"),
                },
                "provider_window_limits": {
                    "five_hour_remaining": None,
                    "weekly_remaining": None,
                    "source": "per_provider_probe",
                    "note": global_note,
                },
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_settings_get(request: Request) -> JSONResponse:
    settings = state.load_settings()
    safe = {k: v for k, v in settings.items()}
    for key in (
        "OPENROUTER_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "LOCAL_MODEL_API_KEY",
    ):
        if safe.get(key):
            safe[key] = safe[key][:8] + "..." if len(safe[key]) > 8 else "***"
    return JSONResponse(safe)


async def api_settings_post(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        current = state.load_settings()
        for key in state.SETTINGS_DEFAULTS:
            if key in body:
                current[key] = body[key]
        state.save_settings(current)
        state._apply_settings_to_env(current)

        warnings = []
        repo_slug = current.get("GITHUB_REPO", "")
        gh_token = current.get("GITHUB_TOKEN", "")
        if repo_slug and gh_token:
            from supervisor.git_ops import configure_remote, migrate_remote_credentials

            remote_ok, remote_msg = configure_remote(repo_slug, gh_token)
            if not remote_ok:
                state.log.warning("Remote configuration failed on settings save: %s", remote_msg)
                warnings.append(f"Remote config failed: {remote_msg}")
            else:
                mig_ok, mig_msg = migrate_remote_credentials()
                if not mig_ok:
                    state.log.warning("Credential migration failed: %s", mig_msg)
        resp = {"status": "saved"}
        if warnings:
            resp["warnings"] = warnings
        return JSONResponse(resp)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_reset(request: Request) -> JSONResponse:
    try:
        deleted = []
        for subdir in ("state", "memory", "logs", "archive", "locks", "task_results"):
            path = state.DATA_DIR / subdir
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                deleted.append(subdir)
        settings_file = state.DATA_DIR / "settings.json"
        if settings_file.exists():
            settings_file.unlink()
            deleted.append("settings.json")
        state.request_restart_exit()
        return JSONResponse({"status": "ok", "deleted": deleted, "restarting": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_command(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        cmd = body.get("cmd", "")
        if cmd:
            from supervisor.message_bus import get_bridge

            get_bridge().ui_send(cmd)
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_git_log(request: Request) -> JSONResponse:
    try:
        from supervisor.git_ops import git_capture, list_commits, list_versions

        commits = list_commits(max_count=30)
        tags = list_versions(max_count=20)
        rc, branch, _ = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        rc2, sha, _ = git_capture(["git", "rev-parse", "--short", "HEAD"])
        return JSONResponse(
            {
                "commits": commits,
                "tags": tags,
                "branch": branch.strip() if rc == 0 else "unknown",
                "sha": sha.strip() if rc2 == 0 else "",
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_git_rollback(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        target = body.get("target", "").strip()
        if not target:
            return JSONResponse({"error": "missing target"}, status_code=400)
        from supervisor.git_ops import rollback_to_version

        ok, msg = rollback_to_version(target, reason="ui_rollback")
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
        state.request_restart_exit()
        return JSONResponse({"status": "ok", "message": msg})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_git_promote(request: Request) -> JSONResponse:
    try:
        subprocess.run(
            ["git", "branch", "-f", "ouroboros-stable", "ouroboros"],
            cwd=str(state.REPO_DIR),
            check=True,
            capture_output=True,
        )
        return JSONResponse(
            {
                "status": "ok",
                "message": "ouroboros-stable updated to match ouroboros",
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_evolution_data(request: Request) -> JSONResponse:
    from ouroboros.utils import collect_evolution_metrics
    from ouroboros.evolution_archive import summarize_evolution_archive

    now = time.time()
    cache = state.get_evolution_cache()
    if cache.get("ts") and now - cache["ts"] < 60:
        return JSONResponse({"points": cache["points"], "summary": cache.get("summary") or {}})

    data_dir = os.environ.get("OUROBOROS_DATA_DIR", os.path.expanduser("~/Ouroboros/data"))
    data_points = await collect_evolution_metrics(str(state.REPO_DIR), data_dir=data_dir)
    summary = summarize_evolution_archive(data_dir)
    cache["ts"] = now
    cache["points"] = data_points
    cache["summary"] = summary
    return JSONResponse({"points": data_points, "summary": summary})


async def index_page(request: Request) -> FileResponse:
    index = state.REPO_DIR / "web" / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html")
    return HTMLResponse("<html><body><h1>Ouroboros - web/ not found</h1></body></html>", status_code=404)


async def api_cost_breakdown(request: Request) -> JSONResponse:
    from supervisor.state import load_state

    events_path = state.DATA_DIR / "logs" / "events.jsonl"
    by_model: Dict[str, Dict[str, Any]] = {}
    by_api_key: Dict[str, Dict[str, Any]] = {}
    by_model_category: Dict[str, Dict[str, Any]] = {}
    by_task_category: Dict[str, Dict[str, Any]] = {}
    total_cost = 0.0
    total_calls = 0

    def _acc(bucket: Dict[str, Dict[str, Any]], key: str) -> Dict[str, Any]:
        if key not in bucket:
            bucket[key] = {"cost": 0.0, "calls": 0}
        return bucket[key]

    try:
        if events_path.exists():
            with events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue
                    if evt.get("type") != "llm_usage":
                        continue
                    cost = float(evt.get("cost") or 0)
                    model = str(evt.get("model") or "unknown")
                    api_key_type = str(evt.get("api_key_type") or evt.get("provider") or "unknown")
                    model_cat = str(evt.get("model_category") or "other")
                    task_cat = str(evt.get("category") or "task")

                    total_cost += cost
                    total_calls += 1

                    _acc(by_model, model)["cost"] += cost
                    _acc(by_model, model)["calls"] += 1
                    _acc(by_api_key, api_key_type)["cost"] += cost
                    _acc(by_api_key, api_key_type)["calls"] += 1
                    _acc(by_model_category, model_cat)["cost"] += cost
                    _acc(by_model_category, model_cat)["calls"] += 1
                    _acc(by_task_category, task_cat)["cost"] += cost
                    _acc(by_task_category, task_cat)["calls"] += 1
    except Exception:
        pass

    runtime_state = load_state()
    active_provider = str(get_cloud_provider() or "").strip().lower()
    use_calls_metric = bool(
        active_provider == "minimax"
        or (
            float(total_cost or 0.0) == 0.0
            and int(runtime_state.get("minimax_requests_5h_limit") or 0) > 0
        )
    )

    def _sorted(bucket: Dict[str, Dict[str, Any]]) -> dict:
        metric_key = "calls" if use_calls_metric else "cost"
        return dict(
            sorted(
                bucket.items(),
                key=lambda item: (item[1].get(metric_key, 0), item[1].get("calls", 0), item[0]),
                reverse=True,
            )
        )

    sorted_models = _sorted(by_model)
    top_model = next(iter(sorted_models.keys()), "-")
    return JSONResponse(
        {
            "total_cost": round(total_cost, 4),
            "total_calls": total_calls,
            "top_model": top_model,
            "display_metric": "calls" if use_calls_metric else "cost",
            "by_model": sorted_models,
            "by_api_key": _sorted(by_api_key),
            "by_model_category": _sorted(by_model_category),
            "by_task_category": _sorted(by_task_category),
            "minimax_requests_5h_used": int(runtime_state.get("minimax_requests_5h_used") or 0),
            "minimax_requests_5h_limit": int(runtime_state.get("minimax_requests_5h_limit") or 0),
            "minimax_requests_5h_remaining": runtime_state.get("minimax_requests_5h_remaining"),
            "minimax_requests_weekly_used": int(runtime_state.get("minimax_requests_weekly_used") or 0),
            "minimax_requests_weekly_limit": int(runtime_state.get("minimax_requests_weekly_limit") or 0),
            "minimax_requests_weekly_remaining": runtime_state.get("minimax_requests_weekly_remaining"),
        }
    )


async def api_chat_history(request: Request) -> JSONResponse:
    try:
        limit = max(0, min(int(request.query_params.get("limit", 1000)), 2000))
    except (AttributeError, TypeError, ValueError):
        limit = 1000

    combined: list = []

    chat_path = state.DATA_DIR / "logs" / "chat.jsonl"
    if chat_path.exists():
        try:
            with chat_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    direction = str(entry.get("direction", "")).lower()
                    role = {"in": "user", "out": "assistant", "system": "system"}.get(direction)
                    if role is None:
                        continue
                    combined.append(
                        {
                            "text": str(entry.get("text", "")),
                            "role": role,
                            "ts": str(entry.get("ts", "")),
                            "is_progress": False,
                            "system_type": str(entry.get("type", "")),
                            "markdown": str(entry.get("format", "")).lower() == "markdown",
                        }
                    )
        except Exception as exc:
            state.log.warning("Failed to read chat history: %s", exc)

    progress_path = state.DATA_DIR / "logs" / "progress.jsonl"
    if progress_path.exists():
        try:
            with progress_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    text = str(entry.get("content", entry.get("text", "")))
                    if not text:
                        continue
                    combined.append(
                        {
                            "text": text,
                            "role": "assistant",
                            "ts": str(entry.get("ts", "")),
                            "is_progress": True,
                            "markdown": str(entry.get("format", "")).lower() == "markdown",
                        }
                    )
        except Exception as exc:
            state.log.warning("Failed to read progress log: %s", exc)

    combined.sort(key=lambda message: message.get("ts", ""))
    messages = combined[-limit:] if len(combined) > limit else combined
    return JSONResponse({"messages": messages})


async def api_telegram_webhook(request: Request) -> JSONResponse:
    try:
        update = await request.json()
    except Exception:
        state.log.warning("Failed to parse Telegram webhook JSON")
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

    telegram_enabled = os.environ.get("TELEGRAM_BOT_ENABLED", "false").lower() in ("true", "1", "yes")
    if not telegram_enabled:
        state.log.debug("Telegram webhook received but bot is disabled")
        return JSONResponse({"status": "ok"})

    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    msg_id = message.get("message_id")

    if not chat_id or not text:
        state.log.debug("Telegram update without chat_id or text: %s", update.keys())
        return JSONResponse({"status": "ok"})

    state.log.info("Telegram message from chat %s: %s", chat_id, text[:100])
    try:
        from supervisor.message_bus import LocalChatBridge

        task_data = {
            "type": "telegram_message",
            "chat_id": chat_id,
            "text": text,
            "msg_id": msg_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        LocalChatBridge().push_message(task_data)
        return JSONResponse({"status": "ok", "message": "Message queued"})
    except Exception as exc:
        state.log.error("Failed to route Telegram message: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


async def api_telegram_process_message(request: Request) -> JSONResponse:
    secret = request.headers.get("X-Telegram-Secret")
    expected_secret = os.environ.get("TELEGRAM_INTERNAL_SECRET", "")

    if not expected_secret:
        state.log.warning("Telegram internal secret not configured")
        return JSONResponse(
            {"status": "error", "message": "Internal secret not configured"},
            status_code=500,
        )

    if secret != expected_secret:
        state.log.warning("Invalid Telegram internal secret")
        return JSONResponse({"status": "error", "message": "Invalid secret"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        state.log.warning("Failed to parse Telegram process-message JSON")
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

    chat_id = payload.get("chat_id")
    text = payload.get("text")
    message_id = payload.get("message_id")

    if not chat_id or not text:
        state.log.warning("Invalid payload from telegram_bot: %s", payload.keys())
        return JSONResponse(
            {"status": "error", "message": "Missing chat_id or text"},
            status_code=400,
        )

    state.log.info("Telegram process-message from chat %s: %s", chat_id, text[:100])
    try:
        from supervisor.message_bus import LocalChatBridge

        task_data = {
            "type": "telegram_message",
            "chat_id": chat_id,
            "text": text,
            "msg_id": message_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        LocalChatBridge().push_message(task_data)
        return JSONResponse({"status": "success", "message": "Message queued for processing"})
    except Exception as exc:
        state.log.error("Failed to route Telegram message from bot: %s", exc, exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


class NoCacheStaticFiles:
    """Wrap StaticFiles to add Cache-Control: no-cache headers."""

    def __init__(self, **kwargs):
        self._app = StaticFiles(**kwargs)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":

            async def send_with_no_cache(message):
                if message["type"] == "http.response.start":
                    headers = [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.lower() != b"cache-control"
                    ]
                    headers.append((b"cache-control", b"no-cache, must-revalidate"))
                    message = {**message, "headers": headers}
                await send(message)

            await self._app(scope, receive, send_with_no_cache)
            return
        await self._app(scope, receive, send)


def build_routes() -> list:
    web_dir = state.REPO_DIR / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    return [
        Route("/", endpoint=index_page),
        Route("/api/health", endpoint=api_health),
        Route("/api/state", endpoint=api_state),
        Route("/api/executor/status", endpoint=api_executor_status),
        Route("/api/settings", endpoint=api_settings_get, methods=["GET"]),
        Route("/api/settings", endpoint=api_settings_post, methods=["POST"]),
        Route("/api/command", endpoint=api_command, methods=["POST"]),
        Route("/api/reset", endpoint=api_reset, methods=["POST"]),
        Route("/api/git/log", endpoint=api_git_log),
        Route("/api/git/rollback", endpoint=api_git_rollback, methods=["POST"]),
        Route("/api/git/promote", endpoint=api_git_promote, methods=["POST"]),
        Route("/api/cost-breakdown", endpoint=api_cost_breakdown),
        Route("/api/evolution-data", endpoint=api_evolution_data),
        Route("/api/chat/history", endpoint=api_chat_history),
        Route("/api/local-model/start", endpoint=api_local_model_start, methods=["POST"]),
        Route("/api/local-model/stop", endpoint=api_local_model_stop, methods=["POST"]),
        Route("/api/local-model/status", endpoint=api_local_model_status),
        Route("/api/local-model/test", endpoint=api_local_model_test, methods=["POST"]),
        Route("/api/telegram/webhook", endpoint=api_telegram_webhook, methods=["POST"]),
        Route("/api/telegram/process-message", endpoint=api_telegram_process_message, methods=["POST"]),
        WebSocketRoute("/ws", endpoint=websocket.ws_endpoint),
        Mount("/static", app=NoCacheStaticFiles(directory=str(web_dir)), name="static"),
    ]


__all__ = [
    "api_chat_history",
    "api_command",
    "api_cost_breakdown",
    "api_evolution_data",
    "api_executor_status",
    "api_git_log",
    "api_git_promote",
    "api_git_rollback",
    "api_health",
    "api_reset",
    "api_settings_get",
    "api_settings_post",
    "api_state",
    "api_telegram_process_message",
    "api_telegram_webhook",
    "build_routes",
    "index_page",
]
