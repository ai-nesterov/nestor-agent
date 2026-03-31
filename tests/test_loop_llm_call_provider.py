import pathlib
import queue


def test_call_llm_with_retry_uses_minimax_provider_for_cloud_events(monkeypatch, tmp_path):
    from ouroboros.loop_llm_call import call_llm_with_retry

    emitted = []

    class FakeLlm:
        def chat(self, **kwargs):
            return (
                {"content": "ok"},
                {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0},
            )

        def cloud_provider(self):
            return "minimax"

    monkeypatch.setattr(
        "ouroboros.loop_llm_call.emit_llm_usage_event",
        lambda event_queue, task_id, model, usage, cost, category, provider=None, source="loop": emitted.append(
            {"provider": provider, "model": model, "category": category}
        ),
    )

    msg, cost = call_llm_with_retry(
        llm=FakeLlm(),
        messages=[{"role": "user", "content": "hi"}],
        model="MiniMax-M2.7",
        tools=None,
        effort="medium",
        max_retries=1,
        drive_logs=pathlib.Path(tmp_path),
        task_id="task-1",
        round_idx=1,
        event_queue=queue.Queue(),
        accumulated_usage={},
        task_type="task",
        use_local=False,
    )

    assert msg["content"] == "ok"
    assert emitted[0]["provider"] == "minimax"


def test_build_fallback_candidates_prefers_cloud_then_local(monkeypatch):
    from ouroboros.loop import _build_fallback_candidates

    monkeypatch.setattr("ouroboros.loop.get_lane_model", lambda lane, prefer_local=False: "MiniMax-M2.1")
    monkeypatch.setattr("ouroboros.loop.get_local_lane_model", lambda lane: "Qwen/Qwen3.5-27B")
    monkeypatch.setattr("ouroboros.loop.has_local_model_config", lambda: True)

    assert _build_fallback_candidates("MiniMax-M2.5", active_use_local=False) == [
        ("MiniMax-M2.1", False),
        ("Qwen/Qwen3.5-27B", True),
    ]


def test_build_fallback_candidates_for_local_primary_skips_cloud_stage(monkeypatch):
    from ouroboros.loop import _build_fallback_candidates

    monkeypatch.setattr("ouroboros.loop.get_lane_model", lambda lane, prefer_local=False: "MiniMax-M2.1")
    monkeypatch.setattr("ouroboros.loop.get_local_lane_model", lambda lane: "Qwen/Qwen3.5-27B")
    monkeypatch.setattr("ouroboros.loop.has_local_model_config", lambda: True)

    assert _build_fallback_candidates("Qwen/Qwen3.5-27B", active_use_local=True) == []


def test_build_fallback_candidates_skips_cloud_when_quota_blocked(monkeypatch):
    from ouroboros.loop import _build_fallback_candidates

    monkeypatch.setattr("ouroboros.loop.get_lane_model", lambda lane, prefer_local=False: "MiniMax-M2.1")
    monkeypatch.setattr("ouroboros.loop.get_local_lane_model", lambda lane: "Qwen/Qwen3.5-27B")
    monkeypatch.setattr("ouroboros.loop.has_local_model_config", lambda: True)
    monkeypatch.setattr(
        "ouroboros.loop.get_provider_quota_status",
        lambda: {"hard_blocked": True, "soft_limited": False, "reason": "MiniMax 5h quota exhausted"},
    )

    assert _build_fallback_candidates("MiniMax-M2.5", active_use_local=False) == [
        ("Qwen/Qwen3.5-27B", True),
    ]


def test_get_quota_adjusted_effort_downgrades_on_soft_limit(monkeypatch):
    from ouroboros.loop import _get_quota_adjusted_effort

    monkeypatch.setattr(
        "ouroboros.loop.get_provider_quota_status",
        lambda: {"hard_blocked": False, "soft_limited": True, "reason": "MiniMax quota low"},
    )

    status, effort = _get_quota_adjusted_effort("high", active_use_local=False)

    assert status["soft_limited"] is True
    assert effort == "low"
