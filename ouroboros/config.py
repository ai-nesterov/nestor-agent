"""
Ouroboros — Shared configuration (single source of truth).

Paths, settings defaults, load/save with file locking.
Only imports ouroboros.compat (platform abstraction, no circular deps).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Optional

from ouroboros.compat import pid_lock_acquire as _compat_pid_lock_acquire
from ouroboros.compat import pid_lock_release as _compat_pid_lock_release


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME = pathlib.Path.home()
APP_ROOT = HOME / "Ouroboros"
REPO_DIR = APP_ROOT / "repo"
DATA_DIR = APP_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
PID_FILE = APP_ROOT / "ouroboros.pid"
PORT_FILE = DATA_DIR / "state" / "server_port"

RESTART_EXIT_CODE = 42
PANIC_EXIT_CODE = 99
AGENT_SERVER_PORT = 8765
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_LOCAL_MODEL_PORT = 8766
_LANE_MODEL_KEYS = {
    "MAIN": "OUROBOROS_MODEL",
    "CODE": "OUROBOROS_MODEL_CODE",
    "LIGHT": "OUROBOROS_MODEL_LIGHT",
    "FALLBACK": "OUROBOROS_MODEL_FALLBACK",
}
_LANE_LOCAL_MODEL_KEYS = {
    "MAIN": "LOCAL_MODEL_MAIN",
    "CODE": "LOCAL_MODEL_CODE",
    "LIGHT": "LOCAL_MODEL_LIGHT",
    "FALLBACK": "LOCAL_MODEL_FALLBACK",
}


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------
SETTINGS_DEFAULTS = {
    "OPENROUTER_API_KEY": "",
    "OPENROUTER_BASE_URL": DEFAULT_OPENROUTER_BASE_URL,
    "MINIMAX_API_KEY": "",
    "MINIMAX_BASE_URL": DEFAULT_MINIMAX_BASE_URL,
    "MINIMAX_PLAN_TYPE": "token_plan",
    "MINIMAX_PLAN_TIER": "",
    "MINIMAX_REQUESTS_5H_LIMIT": 0,
    "MINIMAX_REQUESTS_WEEKLY_LIMIT": 0,
    "LLM_PROVIDER": "openrouter",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "OUROBOROS_MODEL": "Qwen/Qwen3.5-27B",
    "OUROBOROS_MODEL_CODE": "Qwen/Qwen3-Coder-Next",
    "OUROBOROS_MODEL_LIGHT": "Qwen/Qwen3.5-27B",
    "OUROBOROS_MODEL_CONSOLIDATION": "Openai/Gpt-oss-120b",
    "OUROBOROS_MODEL_FALLBACK": "Openai/Gpt-oss-120b",
    "CLAUDE_CODE_MODEL": "opus",
    "CLAUDE_CODE_AUTH_MODE": "subscription_only",
    "CODEX_MODEL": "gpt-5.4",
    "CODEX_AUTH_MODE": "subscription_only",
    "EXTERNAL_EXECUTORS_ENABLED": False,
    "CLAUDE_CODE_ENABLED": False,
    "CODEX_ENABLED": False,
    "CLAUDE_CODE_WORKERS": 1,
    "CODEX_WORKERS": 1,
    "MAIN_WORKERS": 5,
    "CLAUDE_CODE_MAX_PARALLEL": 1,
    "CODEX_MAX_PARALLEL": 1,
    "CLAUDE_CODE_DAILY_TASK_CAP": 5,
    "CODEX_DAILY_TASK_CAP": 5,
    "CLAUDE_ALLOWED_IN_EVOLUTION": False,
    "CODEX_ALLOWED_IN_EVOLUTION": False,
    "CLAUDE_ALLOWED_IN_REVIEW": True,
    "CODEX_ALLOWED_IN_REVIEW": True,
    "CLAUDE_ALLOWED_IN_CONSCIOUSNESS": False,
    "CODEX_ALLOWED_IN_CONSCIOUSNESS": False,
    "OUROBOROS_MAX_WORKERS": 5,
    "TOTAL_BUDGET": 10.0,
    "OUROBOROS_SOFT_TIMEOUT_SEC": 600,
    "OUROBOROS_HARD_TIMEOUT_SEC": 1800,
    "OUROBOROS_TOOL_TIMEOUT_SEC": 120,
    "OUROBOROS_BG_MAX_ROUNDS": 5,
    "OUROBOROS_BG_WAKEUP_MIN": 30,
    "OUROBOROS_BG_WAKEUP_MAX": 7200,
    "OUROBOROS_EVO_COST_THRESHOLD": 0.10,
    "OUROBOROS_WEBSEARCH_MODEL": "gpt-5.2",
    # Pre-commit review: comma-separated list of OpenRouter model IDs
    "OUROBOROS_REVIEW_MODELS": "openai/gpt-5.4,google/gemini-3.1-pro-preview,anthropic/claude-opus-4.6",
    # Pre-commit review enforcement: advisory | blocking
    "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
    # Pre-commit review backend: cloud | codex | claude_code | both
    "OUROBOROS_REVIEW_EXECUTOR": "cloud",
    # Reasoning effort per task type: none | low | medium | high
    # OUROBOROS_INITIAL_REASONING_EFFORT remains a legacy alias for task/chat.
    "OUROBOROS_EFFORT_TASK": "medium",
    "OUROBOROS_EFFORT_EVOLUTION": "high",
    "OUROBOROS_EFFORT_REVIEW": "medium",
    "OUROBOROS_EFFORT_CONSCIOUSNESS": "low",
    "GITHUB_TOKEN": "",
    "GITHUB_REPO": "",
    # Telegram bot integration
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_BOT_ENABLED": "false",
    "TELEGRAM_WEBHOOK_URL": "",
    "TELEGRAM_INTERNAL_SECRET": "",
    "TELEGRAM_BOT_PORT": 8767,
    "TELEGRAM_ADMIN_CHAT_IDS": [],
    # Local model (llama-cpp-python server)
    "LOCAL_MODEL_SOURCE": "",
    "LOCAL_MODEL_FILENAME": "",
    "LOCAL_MODEL_PORT": DEFAULT_LOCAL_MODEL_PORT,
    "LOCAL_MODEL_BASE_URL": "https://inference.airi.net:46783/v1",
    "LOCAL_MODEL_API_KEY": "",
    "LOCAL_MODEL_N_GPU_LAYERS": 0,
    "LOCAL_MODEL_CONTEXT_LENGTH": 32768,
    "LOCAL_MODEL_CHAT_FORMAT": "",
    "LOCAL_MODEL_MAIN": "",
    "LOCAL_MODEL_CODE": "",
    "LOCAL_MODEL_LIGHT": "",
    "LOCAL_MODEL_FALLBACK": "",
    "USE_LOCAL_MAIN": True,
    "USE_LOCAL_CODE": True,
    "USE_LOCAL_LIGHT": True,
    "USE_LOCAL_FALLBACK": True,
    "EXECUTOR_RUNS_SUBDIR": "executor_runs",
    "EXECUTOR_WORKTREES_SUBDIR": "worktrees",
}

_VALID_EFFORTS = ("none", "low", "medium", "high")
_TRUE_VALUES = ("true", "1", "yes", "on")


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _settings_value(settings: Optional[dict], key: str, default: object = "") -> object:
    if settings is not None:
        return settings.get(key, default)
    return os.environ.get(key, default)


def has_openrouter_config(settings: Optional[dict] = None) -> bool:
    """Return True when cloud OpenRouter backend is configured."""
    return bool(str(_settings_value(settings, "OPENROUTER_API_KEY", "")).strip())


def has_minimax_config(settings: Optional[dict] = None) -> bool:
    """Return True when MiniMax cloud backend is configured."""
    return bool(str(_settings_value(settings, "MINIMAX_API_KEY", "")).strip())


def get_cloud_provider(settings: Optional[dict] = None) -> str:
    """Return the selected cloud provider."""
    raw = str(_settings_value(settings, "LLM_PROVIDER", SETTINGS_DEFAULTS["LLM_PROVIDER"])).strip().lower()
    return raw if raw in {"openrouter", "minimax"} else str(SETTINGS_DEFAULTS["LLM_PROVIDER"])


def has_cloud_provider_config(settings: Optional[dict] = None) -> bool:
    """Return True when the selected cloud provider is configured."""
    provider = get_cloud_provider(settings)
    if provider == "minimax":
        return has_minimax_config(settings)
    return has_openrouter_config(settings)


def has_local_routing_enabled(settings: Optional[dict] = None) -> bool:
    """Return True when any model lane is configured to use local routing."""
    return any(
        _truthy(_settings_value(settings, k, False))
        for k in ("USE_LOCAL_MAIN", "USE_LOCAL_CODE", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK")
    )


def has_local_model_config(settings: Optional[dict] = None) -> bool:
    """Return True when local model backend is configured via base URL or legacy path."""
    explicit_base = bool(str(_settings_value(settings, "LOCAL_MODEL_BASE_URL", "")).strip())
    if explicit_base:
        return True

    local_source = bool(str(_settings_value(settings, "LOCAL_MODEL_SOURCE", "")).strip())
    routing_enabled = has_local_routing_enabled(settings)
    port = str(_settings_value(settings, "LOCAL_MODEL_PORT", "")).strip()
    return bool(port) and (local_source or routing_enabled)


def has_configured_llm_backend(settings: Optional[dict] = None) -> bool:
    """Return True when any usable LLM backend is configured."""
    return has_cloud_provider_config(settings) or has_local_model_config(settings)


def use_local_for_lane(lane: str, settings: Optional[dict] = None) -> bool:
    """Resolve whether a lane should route to local backend.

    Preserves explicit USE_LOCAL_* behavior. If cloud is unavailable but a local
    backend is configured, auto-falls back to local for core continuity.
    """
    lane_key = f"USE_LOCAL_{str(lane or '').upper()}"
    if _truthy(_settings_value(settings, lane_key, False)):
        return True
    if has_cloud_provider_config(settings):
        return False
    return has_local_model_config(settings)


def get_lane_model(lane: str, settings: Optional[dict] = None, prefer_local: Optional[bool] = None) -> str:
    """Return the configured model for a lane, using local overrides when requested."""
    lane_name = str(lane or "").upper().strip()
    cloud_key = _LANE_MODEL_KEYS.get(lane_name, "OUROBOROS_MODEL")
    local_key = _LANE_LOCAL_MODEL_KEYS.get(lane_name, "LOCAL_MODEL_MAIN")

    cloud_default = str(SETTINGS_DEFAULTS.get(cloud_key, SETTINGS_DEFAULTS["OUROBOROS_MODEL"]))
    cloud_model = str(_settings_value(settings, cloud_key, cloud_default) or "").strip() or cloud_default
    local_model = str(_settings_value(settings, local_key, "") or "").strip()

    if prefer_local is None:
        prefer_local = use_local_for_lane(lane_name, settings)
    if prefer_local and local_model:
        return local_model
    return cloud_model


def get_local_lane_model(lane: str, settings: Optional[dict] = None) -> str:
    """Return the best local model ID for a lane.

    Preference order:
    1) explicit LOCAL_MODEL_<LANE>
    2) LOCAL_MODEL_MAIN for non-main lanes
    3) cloud lane model as last-resort compatibility fallback
    """
    lane_name = str(lane or "").upper().strip()
    local_key = _LANE_LOCAL_MODEL_KEYS.get(lane_name, "LOCAL_MODEL_MAIN")
    local_model = str(_settings_value(settings, local_key, "") or "").strip()
    if local_model:
        return local_model
    if lane_name != "MAIN":
        main_local = str(_settings_value(settings, "LOCAL_MODEL_MAIN", "") or "").strip()
        if main_local:
            return main_local
    return get_lane_model(lane_name, settings=settings, prefer_local=False)


def resolve_effort(task_type: str) -> str:
    """Return the configured reasoning effort for the given task type."""
    t = (task_type or "").lower().strip()

    if t == "evolution":
        key = "OUROBOROS_EFFORT_EVOLUTION"
        default = "high"
    elif t == "review":
        key = "OUROBOROS_EFFORT_REVIEW"
        default = "medium"
    elif t == "consciousness":
        key = "OUROBOROS_EFFORT_CONSCIOUSNESS"
        default = "low"
    else:
        legacy = os.environ.get("OUROBOROS_INITIAL_REASONING_EFFORT", "")
        key = "OUROBOROS_EFFORT_TASK"
        default = legacy if legacy in _VALID_EFFORTS else "medium"

    raw = os.environ.get(key, default)
    return raw if raw in _VALID_EFFORTS else default


def get_review_models() -> list[str]:
    """Return the configured pre-commit review model list."""
    default_str = SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"]
    models_str = os.environ.get("OUROBOROS_REVIEW_MODELS", default_str) or default_str
    return [m.strip() for m in models_str.split(",") if m.strip()]


def get_review_enforcement() -> str:
    """Return the configured pre-commit review enforcement mode."""
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"])
    raw = (os.environ.get("OUROBOROS_REVIEW_ENFORCEMENT", default_val) or default_val).strip().lower()
    return raw if raw in {"advisory", "blocking"} else default_val


def get_review_executor() -> str:
    """Return the configured pre-commit review executor."""
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_EXECUTOR"])
    raw = (os.environ.get("OUROBOROS_REVIEW_EXECUTOR", default_val) or default_val).strip().lower()
    return raw if raw in {"cloud", "codex", "claude_code", "both"} else default_val


def resolve_openrouter_base_url(base_url: Optional[str] = None) -> str:
    """Resolve OpenRouter base URL from arg -> env -> defaults."""
    if base_url is not None and str(base_url).strip():
        return str(base_url).strip().rstrip("/")
    env_val = os.environ.get("OPENROUTER_BASE_URL", "")
    if env_val.strip():
        return env_val.strip().rstrip("/")
    return str(DEFAULT_OPENROUTER_BASE_URL).rstrip("/")


def resolve_minimax_base_url(base_url: Optional[str] = None) -> str:
    """Resolve MiniMax base URL from arg -> env -> defaults."""
    if base_url is not None and str(base_url).strip():
        return str(base_url).strip().rstrip("/")
    env_val = os.environ.get("MINIMAX_BASE_URL", "")
    if env_val.strip():
        return env_val.strip().rstrip("/")
    return str(DEFAULT_MINIMAX_BASE_URL).rstrip("/")


def resolve_local_model_base_url(base_url: Optional[str] = None, port: Optional[int] = None) -> str:
    """Resolve local model OpenAI-compatible base URL with legacy fallback."""
    if base_url is not None and str(base_url).strip():
        return str(base_url).strip().rstrip("/")
    env_val = os.environ.get("LOCAL_MODEL_BASE_URL", "")
    if env_val.strip():
        return env_val.strip().rstrip("/")
    resolved_port = int(port or os.environ.get("LOCAL_MODEL_PORT", str(DEFAULT_LOCAL_MODEL_PORT)))
    return f"http://127.0.0.1:{resolved_port}/v1"


def resolve_local_model_api_key(api_key: Optional[str] = None) -> str:
    """Resolve local model API key from arg -> env."""
    if api_key is not None:
        return str(api_key)
    return str(os.environ.get("LOCAL_MODEL_API_KEY", ""))


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
def read_version() -> str:
    try:
        if getattr(sys, "frozen", False):
            vp = pathlib.Path(sys._MEIPASS) / "VERSION"
        else:
            vp = pathlib.Path(__file__).parent.parent / "VERSION"
        return vp.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


# ---------------------------------------------------------------------------
# Settings file locking
# ---------------------------------------------------------------------------
_SETTINGS_LOCK = pathlib.Path(str(SETTINGS_PATH) + ".lock")


def _acquire_settings_lock(timeout: float = 2.0) -> Optional[int]:
    start = time.time()
    while time.time() - start < timeout:
        try:
            fd = os.open(str(_SETTINGS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            return fd
        except FileExistsError:
            try:
                if time.time() - _SETTINGS_LOCK.stat().st_mtime > 10:
                    _SETTINGS_LOCK.unlink()
                    continue
            except Exception:
                pass
            time.sleep(0.01)
        except Exception:
            break
    return None


def _release_settings_lock(fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        _SETTINGS_LOCK.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    fd = _acquire_settings_lock()
    try:
        merged_defaults = dict(SETTINGS_DEFAULTS)
        if SETTINGS_PATH.exists():
            try:
                loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merged_defaults.update(loaded)
                    return merged_defaults
            except Exception:
                pass
        return merged_defaults
    finally:
        _release_settings_lock(fd)


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = _acquire_settings_lock()
    try:
        try:
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(SETTINGS_PATH))
        except OSError:
            SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    finally:
        _release_settings_lock(fd)


def apply_settings_to_env(settings: dict) -> None:
    """Push settings into environment variables for supervisor modules."""
    env_keys = [
        "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
        "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_PLAN_TYPE", "MINIMAX_PLAN_TIER",
        "MINIMAX_REQUESTS_5H_LIMIT", "MINIMAX_REQUESTS_WEEKLY_LIMIT", "LLM_PROVIDER",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "OUROBOROS_MODEL", "OUROBOROS_MODEL_CODE", "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_CONSOLIDATION",
        "OUROBOROS_MODEL_FALLBACK", "CLAUDE_CODE_MODEL", "CLAUDE_CODE_AUTH_MODE",
        "CODEX_MODEL", "CODEX_AUTH_MODE",
        "TOTAL_BUDGET", "GITHUB_TOKEN", "GITHUB_REPO",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_ENABLED", "TELEGRAM_WEBHOOK_URL",
        "TELEGRAM_INTERNAL_SECRET", "TELEGRAM_BOT_PORT",
        "OUROBOROS_TOOL_TIMEOUT_SEC",
        "OUROBOROS_BG_MAX_ROUNDS", "OUROBOROS_BG_WAKEUP_MIN", "OUROBOROS_BG_WAKEUP_MAX",
        "OUROBOROS_EVO_COST_THRESHOLD", "OUROBOROS_WEBSEARCH_MODEL",
        "OUROBOROS_REVIEW_MODELS", "OUROBOROS_REVIEW_ENFORCEMENT", "OUROBOROS_REVIEW_EXECUTOR",
        "OUROBOROS_EFFORT_TASK", "OUROBOROS_EFFORT_EVOLUTION",
        "OUROBOROS_EFFORT_REVIEW", "OUROBOROS_EFFORT_CONSCIOUSNESS",
        "EXTERNAL_EXECUTORS_ENABLED", "CLAUDE_CODE_ENABLED", "CODEX_ENABLED",
        "MAIN_WORKERS", "CLAUDE_CODE_WORKERS", "CODEX_WORKERS",
        "CLAUDE_CODE_MAX_PARALLEL", "CODEX_MAX_PARALLEL",
        "CLAUDE_CODE_DAILY_TASK_CAP", "CODEX_DAILY_TASK_CAP",
        "CLAUDE_ALLOWED_IN_EVOLUTION", "CODEX_ALLOWED_IN_EVOLUTION",
        "CLAUDE_ALLOWED_IN_REVIEW", "CODEX_ALLOWED_IN_REVIEW",
        "CLAUDE_ALLOWED_IN_CONSCIOUSNESS", "CODEX_ALLOWED_IN_CONSCIOUSNESS",
        "LOCAL_MODEL_SOURCE", "LOCAL_MODEL_FILENAME",
        "LOCAL_MODEL_BASE_URL", "LOCAL_MODEL_API_KEY",
        "LOCAL_MODEL_PORT", "LOCAL_MODEL_N_GPU_LAYERS", "LOCAL_MODEL_CONTEXT_LENGTH",
        "LOCAL_MODEL_CHAT_FORMAT",
        "LOCAL_MODEL_MAIN", "LOCAL_MODEL_CODE", "LOCAL_MODEL_LIGHT", "LOCAL_MODEL_FALLBACK",
        "USE_LOCAL_MAIN", "USE_LOCAL_CODE", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK",
        "EXECUTOR_RUNS_SUBDIR", "EXECUTOR_WORKTREES_SUBDIR",
    ]
    for k in env_keys:
        val = settings.get(k)
        if val is None or val == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(val)
    if not os.environ.get("OUROBOROS_REVIEW_MODELS"):
        os.environ["OUROBOROS_REVIEW_MODELS"] = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"])
    if not os.environ.get("OUROBOROS_REVIEW_ENFORCEMENT"):
        os.environ["OUROBOROS_REVIEW_ENFORCEMENT"] = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"])
    if not os.environ.get("OUROBOROS_REVIEW_EXECUTOR"):
        os.environ["OUROBOROS_REVIEW_EXECUTOR"] = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_EXECUTOR"])


# ---------------------------------------------------------------------------
# PID lock (single instance) — crash-proof locking via ouroboros.compat.
# On Unix the OS releases flock automatically when the process dies
# (even SIGKILL), so stale lock files can never block future launches.
# On Windows msvcrt.locking provides equivalent semantics.
# ---------------------------------------------------------------------------

def acquire_pid_lock() -> bool:
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    return _compat_pid_lock_acquire(str(PID_FILE))


def release_pid_lock() -> None:
    _compat_pid_lock_release(str(PID_FILE))
