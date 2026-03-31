import json


def test_cost_breakdown_unknown_provider_is_not_labeled_openrouter(tmp_path, monkeypatch):
    import server

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "events.jsonl").write_text(
        json.dumps({
            "type": "llm_usage",
            "model": "MiniMax-M2.7",
            "cost": 0.0,
        }) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "load_state", lambda: {}, raising=False)

    response = __import__("asyncio").run(server.api_cost_breakdown(None))
    payload = json.loads(response.body.decode("utf-8"))

    assert "unknown" in payload["by_api_key"]
    assert "openrouter" not in payload["by_api_key"]
