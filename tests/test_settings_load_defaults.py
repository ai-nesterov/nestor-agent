import json

from ouroboros import config


def test_load_settings_merges_defaults_for_missing_keys(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    lock_path = tmp_path / "settings.json.lock"
    settings_path.write_text(
        json.dumps(
            {
                "EXTERNAL_EXECUTORS_ENABLED": True,
                "CODEX_ENABLED": True,
                "CLAUDE_CODE_ENABLED": True,
                # Intentionally missing MAIN_WORKERS/CODEX_WORKERS/CLAUDE_CODE_WORKERS.
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(config, "_SETTINGS_LOCK", lock_path)

    loaded = config.load_settings()

    assert loaded["EXTERNAL_EXECUTORS_ENABLED"] is True
    assert loaded["CODEX_ENABLED"] is True
    assert loaded["CLAUDE_CODE_ENABLED"] is True
    assert loaded["MAIN_WORKERS"] == config.SETTINGS_DEFAULTS["MAIN_WORKERS"]
    assert loaded["CODEX_WORKERS"] == config.SETTINGS_DEFAULTS["CODEX_WORKERS"]
    assert loaded["CLAUDE_CODE_WORKERS"] == config.SETTINGS_DEFAULTS["CLAUDE_CODE_WORKERS"]
