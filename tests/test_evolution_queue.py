import datetime


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _iso_ago(seconds: int) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    ).isoformat()


def test_build_evolution_task_text_is_autonomous():
    from supervisor import queue as q_module

    text = q_module.build_evolution_task_text(12)

    assert "EVOLUTION #12" in text
    assert "[SPECIFY CONCRETE GOAL]" not in text
    assert "Autonomous cycle" in text


def test_enqueue_evolution_waits_for_owner_after_owner_input(monkeypatch):
    from supervisor import queue as q_module

    pending = []
    running = {}
    seq = {"value": 0}
    q_module.init_queue_refs(pending, running, seq)

    state = {
        "evolution_mode_enabled": True,
        "owner_chat_id": 1,
        "evolution_consecutive_failures": 0,
        "evolution_waiting_for_owner": True,
        "evolution_last_outcome": "needs_owner_input",
        "evolution_last_outcome_at": _iso_now(),
        "last_owner_message_at": _iso_ago(60),
    }
    saved = []
    sent = []

    monkeypatch.setattr(q_module, "load_state", lambda: dict(state))
    monkeypatch.setattr(q_module, "save_state", lambda st: saved.append(dict(st)))
    monkeypatch.setattr(q_module, "send_with_budget", lambda chat_id, text: sent.append((chat_id, text)))

    q_module.enqueue_evolution_task_if_needed()

    assert pending == []
    assert sent == []
    assert saved == []


def test_enqueue_evolution_resumes_after_new_owner_message(monkeypatch):
    from supervisor import queue as q_module

    pending = []
    running = {}
    seq = {"value": 0}
    q_module.init_queue_refs(pending, running, seq)

    state = {
        "evolution_mode_enabled": True,
        "owner_chat_id": 1,
        "evolution_cycle": 4,
        "evolution_consecutive_failures": 0,
        "evolution_waiting_for_owner": True,
        "evolution_last_outcome": "needs_owner_input",
        "evolution_last_outcome_at": _iso_ago(120),
        "last_owner_message_at": _iso_ago(10),
    }
    saved = []
    sent = []

    monkeypatch.setattr(q_module, "load_state", lambda: dict(state))
    monkeypatch.setattr(q_module, "save_state", lambda st: saved.append(dict(st)))
    monkeypatch.setattr(q_module, "send_with_budget", lambda chat_id, text: sent.append((chat_id, text)))

    q_module.enqueue_evolution_task_if_needed()

    assert len(pending) == 1
    assert pending[0]["type"] == "evolution"
    assert any(s.get("evolution_waiting_for_owner") is False for s in saved)
    assert sent and "Evolution #5" in sent[0][1]


def test_enqueue_evolution_respects_idle_cooldown(monkeypatch):
    from supervisor import queue as q_module

    pending = []
    running = {}
    seq = {"value": 0}
    q_module.init_queue_refs(pending, running, seq)

    state = {
        "evolution_mode_enabled": True,
        "owner_chat_id": 1,
        "evolution_cycle": 9,
        "evolution_consecutive_failures": 0,
        "evolution_waiting_for_owner": False,
        "evolution_last_outcome": "no_actionable_goal",
        "evolution_last_outcome_at": _iso_ago(30),
        "last_owner_message_at": _iso_ago(300),
    }

    monkeypatch.setenv("OUROBOROS_EVOLUTION_IDLE_COOLDOWN_SEC", "300")
    monkeypatch.setattr(q_module, "load_state", lambda: dict(state))
    monkeypatch.setattr(q_module, "save_state", lambda st: None)
    monkeypatch.setattr(q_module, "send_with_budget", lambda chat_id, text: None)

    q_module.enqueue_evolution_task_if_needed()

    assert pending == []
