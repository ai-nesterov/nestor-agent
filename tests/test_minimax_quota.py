import datetime


def test_update_budget_tracks_minimax_rolling_5h_requests(tmp_path, monkeypatch):
    from supervisor import state as st_module

    st_module.init(tmp_path, total_budget_limit=10.0)
    monkeypatch.setenv("MINIMAX_REQUESTS_5H_LIMIT", "3")
    monkeypatch.setenv("MINIMAX_REQUESTS_WEEKLY_LIMIT", "10")

    for _ in range(2):
        st_module.update_budget_from_usage({
            "provider": "minimax",
            "cost": 0.0,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        })

    st = st_module.load_state()
    assert len(st.get("minimax_requests_5h_timestamps") or []) == 2
    assert st.get("minimax_requests_5h_used") == 2
    assert st.get("minimax_requests_5h_limit") == 3
    assert st.get("minimax_requests_5h_remaining") == 1
    assert len(st.get("minimax_requests_weekly_timestamps") or []) == 2
    assert st.get("minimax_requests_weekly_used") == 2
    assert st.get("minimax_requests_weekly_limit") == 10
    assert st.get("minimax_requests_weekly_remaining") == 8


def test_update_budget_prunes_old_minimax_rolling_requests(tmp_path, monkeypatch):
    from supervisor import state as st_module

    st_module.init(tmp_path, total_budget_limit=10.0)
    monkeypatch.setenv("MINIMAX_REQUESTS_5H_LIMIT", "5")
    monkeypatch.setenv("MINIMAX_REQUESTS_WEEKLY_LIMIT", "7")
    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)).isoformat()
    old_weekly_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=8)).isoformat()
    st = st_module.load_state()
    st["minimax_requests_5h_timestamps"] = [old_ts]
    st["minimax_requests_weekly_timestamps"] = [old_weekly_ts]
    st_module.save_state(st)

    st_module.update_budget_from_usage({
        "provider": "minimax",
        "cost": 0.0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    })

    updated = st_module.load_state()
    timestamps = updated.get("minimax_requests_5h_timestamps") or []
    assert len(timestamps) == 1
    assert timestamps[0] != old_ts
    assert updated.get("minimax_requests_5h_used") == 1
    weekly_timestamps = updated.get("minimax_requests_weekly_timestamps") or []
    assert len(weekly_timestamps) == 1
    assert weekly_timestamps[0] != old_weekly_ts
    assert updated.get("minimax_requests_weekly_used") == 1


def test_get_provider_quota_status_hard_blocks_when_5h_exhausted(tmp_path, monkeypatch):
    from supervisor import state as st_module

    st_module.init(tmp_path, total_budget_limit=10.0)
    monkeypatch.setenv("MINIMAX_REQUESTS_5H_LIMIT", "2")
    monkeypatch.setenv("MINIMAX_REQUESTS_WEEKLY_LIMIT", "10")
    st = st_module.load_state()
    st["minimax_requests_5h_remaining"] = 0
    st["minimax_requests_weekly_remaining"] = 7
    st_module.save_state(st)

    status = st_module.get_provider_quota_status("minimax")

    assert status["hard_blocked"] is True
    assert "5h quota exhausted" in status["reason"]


def test_get_provider_quota_status_soft_limits_when_remaining_is_low(tmp_path, monkeypatch):
    from supervisor import state as st_module

    st_module.init(tmp_path, total_budget_limit=10.0)
    monkeypatch.setenv("MINIMAX_REQUESTS_5H_LIMIT", "100")
    monkeypatch.setenv("MINIMAX_REQUESTS_WEEKLY_LIMIT", "1000")
    st = st_module.load_state()
    st["minimax_requests_5h_remaining"] = 10
    st["minimax_requests_weekly_remaining"] = 200
    st_module.save_state(st)

    status = st_module.get_provider_quota_status("minimax")

    assert status["hard_blocked"] is False
    assert status["soft_limited"] is True
    assert "5h quota low" in status["reason"]
