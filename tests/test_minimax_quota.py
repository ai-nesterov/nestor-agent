import datetime


def test_update_budget_tracks_minimax_rolling_5h_requests(tmp_path, monkeypatch):
    from supervisor import state as st_module

    st_module.init(tmp_path, total_budget_limit=10.0)
    monkeypatch.setenv("MINIMAX_REQUESTS_5H_LIMIT", "3")

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


def test_update_budget_prunes_old_minimax_rolling_requests(tmp_path, monkeypatch):
    from supervisor import state as st_module

    st_module.init(tmp_path, total_budget_limit=10.0)
    monkeypatch.setenv("MINIMAX_REQUESTS_5H_LIMIT", "5")
    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)).isoformat()
    st = st_module.load_state()
    st["minimax_requests_5h_timestamps"] = [old_ts]
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
