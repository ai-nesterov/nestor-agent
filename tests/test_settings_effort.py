"""Tests for effort, review models, and review enforcement settings."""
import os
from ouroboros.config import (
    SETTINGS_DEFAULTS,
    apply_settings_to_env,
    resolve_effort,
    get_review_models,
    get_review_enforcement,
    get_review_executor,
    has_openrouter_config,
    has_local_model_config,
    has_configured_llm_backend,
    use_local_for_lane,
)


# ---------------------------------------------------------------------------
# Legacy env var backward compat
# ---------------------------------------------------------------------------

def test_initial_effort_default(monkeypatch):
    """Default effort is 'medium' when env var not set."""
    monkeypatch.delenv("OUROBOROS_EFFORT_TASK", raising=False)
    monkeypatch.delenv("OUROBOROS_INITIAL_REASONING_EFFORT", raising=False)
    assert resolve_effort("task") == "medium"


def test_initial_effort_valid_values(monkeypatch):
    """Valid effort values pass through unchanged via OUROBOROS_EFFORT_TASK."""
    for effort in ("none", "low", "medium", "high"):
        monkeypatch.setenv("OUROBOROS_EFFORT_TASK", effort)
        monkeypatch.delenv("OUROBOROS_INITIAL_REASONING_EFFORT", raising=False)
        assert resolve_effort("task") == effort


def test_initial_effort_invalid_falls_back_to_medium(monkeypatch):
    """Invalid effort values fall back to 'medium'."""
    monkeypatch.setenv("OUROBOROS_EFFORT_TASK", "extreme")
    monkeypatch.delenv("OUROBOROS_INITIAL_REASONING_EFFORT", raising=False)
    assert resolve_effort("task") == "medium"


# ---------------------------------------------------------------------------
# New per-type defaults in SETTINGS_DEFAULTS
# ---------------------------------------------------------------------------

def test_effort_defaults_in_config():
    """All four effort keys have correct defaults in SETTINGS_DEFAULTS."""
    assert SETTINGS_DEFAULTS.get("OUROBOROS_EFFORT_TASK") == "medium"
    assert SETTINGS_DEFAULTS.get("OUROBOROS_EFFORT_EVOLUTION") == "high"
    assert SETTINGS_DEFAULTS.get("OUROBOROS_EFFORT_REVIEW") == "medium"
    assert SETTINGS_DEFAULTS.get("OUROBOROS_EFFORT_CONSCIOUSNESS") == "low"


def test_base_url_and_local_api_defaults_in_config():
    """Provider/base-url defaults are present and backward-compatible."""
    assert SETTINGS_DEFAULTS.get("OPENROUTER_BASE_URL") == "https://openrouter.ai/api/v1"
    assert isinstance(SETTINGS_DEFAULTS.get("LOCAL_MODEL_BASE_URL"), str)
    assert SETTINGS_DEFAULTS.get("LOCAL_MODEL_BASE_URL", "").endswith("/v1")
    assert SETTINGS_DEFAULTS.get("LOCAL_MODEL_API_KEY") == ""


def test_review_models_default_in_config():
    """OUROBOROS_REVIEW_MODELS has a default value in config."""
    val = SETTINGS_DEFAULTS.get("OUROBOROS_REVIEW_MODELS", "")
    assert val  # non-empty
    models = [m.strip() for m in val.split(",") if m.strip()]
    assert len(models) >= 2  # quorum requires at least 2


def test_review_enforcement_default_in_config():
    """OUROBOROS_REVIEW_ENFORCEMENT defaults to advisory."""
    assert SETTINGS_DEFAULTS.get("OUROBOROS_REVIEW_ENFORCEMENT") == "advisory"


def test_review_executor_default_in_config():
    """OUROBOROS_REVIEW_EXECUTOR defaults to cloud for backward compatibility."""
    assert SETTINGS_DEFAULTS.get("OUROBOROS_REVIEW_EXECUTOR") == "cloud"


# ---------------------------------------------------------------------------
# get_review_models() — single source of truth
# ---------------------------------------------------------------------------

def test_get_review_models_default(monkeypatch):
    """get_review_models() returns the config default when env is unset."""
    monkeypatch.delenv("OUROBOROS_REVIEW_MODELS", raising=False)
    models = get_review_models()
    assert isinstance(models, list)
    assert len(models) >= 2
    assert all("/" in m for m in models)  # valid OpenRouter model IDs


def test_get_review_models_custom(monkeypatch):
    """get_review_models() returns custom models when env is set."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MODELS", "a/b,c/d")
    models = get_review_models()
    assert models == ["a/b", "c/d"]


def test_get_review_models_empty_env_falls_back_to_default(monkeypatch):
    """get_review_models() falls back to default when env is empty string."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MODELS", "")
    models = get_review_models()
    # Must return the default, not an empty list
    assert len(models) >= 2
    assert models == [m.strip() for m in SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"].split(",") if m.strip()]


def test_get_review_enforcement_default(monkeypatch):
    """get_review_enforcement() returns the config default when env is unset."""
    monkeypatch.delenv("OUROBOROS_REVIEW_ENFORCEMENT", raising=False)
    assert get_review_enforcement() == "advisory"


def test_get_review_enforcement_custom(monkeypatch):
    """get_review_enforcement() accepts advisory and blocking."""
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    assert get_review_enforcement() == "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    assert get_review_enforcement() == "blocking"


def test_get_review_enforcement_invalid_falls_back(monkeypatch):
    """Unknown values fall back to advisory (the default)."""
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "strictest")
    assert get_review_enforcement() == "advisory"


def test_get_review_executor_default(monkeypatch):
    monkeypatch.delenv("OUROBOROS_REVIEW_EXECUTOR", raising=False)
    assert get_review_executor() == "cloud"


def test_get_review_executor_custom(monkeypatch):
    for value in ("cloud", "codex", "claude_code", "both"):
        monkeypatch.setenv("OUROBOROS_REVIEW_EXECUTOR", value)
        assert get_review_executor() == value


def test_get_review_executor_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_EXECUTOR", "nope")
    assert get_review_executor() == "cloud"


def test_apply_settings_clears_review_models_restores_default(monkeypatch):
    """Clearing OUROBOROS_REVIEW_MODELS in settings restores the default in env."""
    # Simulate user clearing the field in Settings UI (empty string)
    settings = {"OUROBOROS_REVIEW_MODELS": ""}
    apply_settings_to_env(settings)
    # env var should be the default, not empty
    env_val = os.environ.get("OUROBOROS_REVIEW_MODELS", "")
    assert env_val == SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"]
    # get_review_models() should also return correct defaults
    assert len(get_review_models()) >= 2


def test_apply_settings_clears_review_enforcement_restores_default(monkeypatch):
    """Clearing OUROBOROS_REVIEW_ENFORCEMENT restores the default in env."""
    settings = {"OUROBOROS_REVIEW_ENFORCEMENT": ""}
    apply_settings_to_env(settings)
    env_val = os.environ.get("OUROBOROS_REVIEW_ENFORCEMENT", "")
    assert env_val == SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"]
    assert get_review_enforcement() == "advisory"


def test_apply_settings_clears_review_executor_restores_default(monkeypatch):
    settings = {"OUROBOROS_REVIEW_EXECUTOR": ""}
    apply_settings_to_env(settings)
    env_val = os.environ.get("OUROBOROS_REVIEW_EXECUTOR", "")
    assert env_val == SETTINGS_DEFAULTS["OUROBOROS_REVIEW_EXECUTOR"]
    assert get_review_executor() == "cloud"


# ---------------------------------------------------------------------------
# apply_settings_to_env propagation
# ---------------------------------------------------------------------------

def test_apply_settings_to_env_includes_effort_keys():
    """apply_settings_to_env propagates all four effort keys."""
    settings = {
        "OUROBOROS_EFFORT_TASK": "low",
        "OUROBOROS_EFFORT_EVOLUTION": "medium",
        "OUROBOROS_EFFORT_REVIEW": "high",
        "OUROBOROS_EFFORT_CONSCIOUSNESS": "none",
        "OUROBOROS_REVIEW_MODELS": "model-a,model-b",
        "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
        "OUROBOROS_REVIEW_EXECUTOR": "both",
    }
    apply_settings_to_env(settings)
    assert os.environ.get("OUROBOROS_EFFORT_TASK") == "low"
    assert os.environ.get("OUROBOROS_EFFORT_EVOLUTION") == "medium"
    assert os.environ.get("OUROBOROS_EFFORT_REVIEW") == "high"
    assert os.environ.get("OUROBOROS_EFFORT_CONSCIOUSNESS") == "none"
    assert os.environ.get("OUROBOROS_REVIEW_MODELS") == "model-a,model-b"
    assert os.environ.get("OUROBOROS_REVIEW_ENFORCEMENT") == "advisory"
    assert os.environ.get("OUROBOROS_REVIEW_EXECUTOR") == "both"
    # cleanup
    for k in ("OUROBOROS_EFFORT_TASK", "OUROBOROS_EFFORT_EVOLUTION",
              "OUROBOROS_EFFORT_REVIEW", "OUROBOROS_EFFORT_CONSCIOUSNESS",
              "OUROBOROS_REVIEW_MODELS", "OUROBOROS_REVIEW_ENFORCEMENT",
              "OUROBOROS_REVIEW_EXECUTOR"):
        os.environ.pop(k, None)


def test_apply_settings_to_env_includes_new_base_url_keys():
    settings = {
        "OPENROUTER_BASE_URL": "https://proxy.example/api/v1",
        "LOCAL_MODEL_BASE_URL": "http://localhost:9999/v1",
        "LOCAL_MODEL_API_KEY": "local-key",
    }
    apply_settings_to_env(settings)
    assert os.environ.get("OPENROUTER_BASE_URL") == "https://proxy.example/api/v1"
    assert os.environ.get("LOCAL_MODEL_BASE_URL") == "http://localhost:9999/v1"
    assert os.environ.get("LOCAL_MODEL_API_KEY") == "local-key"
    for k in ("OPENROUTER_BASE_URL", "LOCAL_MODEL_BASE_URL", "LOCAL_MODEL_API_KEY"):
        os.environ.pop(k, None)


def test_llm_backend_scenario_a_openrouter_only():
    settings = {
        "OPENROUTER_API_KEY": "sk-or-123",
        "LOCAL_MODEL_BASE_URL": "",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_PORT": "",
        "USE_LOCAL_MAIN": False,
    }
    assert has_openrouter_config(settings) is True
    assert has_local_model_config(settings) is False
    assert has_configured_llm_backend(settings) is True
    assert use_local_for_lane("MAIN", settings) is False


def test_llm_backend_scenario_b_local_base_url_only():
    settings = {
        "OPENROUTER_API_KEY": "",
        "LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_PORT": "",
        "USE_LOCAL_MAIN": False,
    }
    assert has_openrouter_config(settings) is False
    assert has_local_model_config(settings) is True
    assert has_configured_llm_backend(settings) is True
    assert use_local_for_lane("MAIN", settings) is True


def test_llm_backend_scenario_b_legacy_local_port_path():
    settings = {
        "OPENROUTER_API_KEY": "",
        "LOCAL_MODEL_BASE_URL": "",
        "LOCAL_MODEL_SOURCE": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "LOCAL_MODEL_PORT": 8766,
        "USE_LOCAL_MAIN": True,
    }
    assert has_openrouter_config(settings) is False
    assert has_local_model_config(settings) is True
    assert has_configured_llm_backend(settings) is True
    assert use_local_for_lane("MAIN", settings) is True


def test_llm_backend_scenario_c_both_configured_defaults_to_cloud_unless_explicit_local():
    settings = {
        "OPENROUTER_API_KEY": "sk-or-123",
        "LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_PORT": "",
        "USE_LOCAL_MAIN": False,
    }
    assert has_configured_llm_backend(settings) is True
    assert use_local_for_lane("MAIN", settings) is False

    settings["USE_LOCAL_MAIN"] = True
    assert use_local_for_lane("MAIN", settings) is True


def test_llm_backend_scenario_d_none_configured():
    settings = {
        "OPENROUTER_API_KEY": "",
        "LOCAL_MODEL_BASE_URL": "",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_PORT": "",
        "USE_LOCAL_MAIN": False,
    }
    assert has_openrouter_config(settings) is False
    assert has_local_model_config(settings) is False
    assert has_configured_llm_backend(settings) is False
