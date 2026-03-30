import json
from types import SimpleNamespace


def test_schedule_task_creates_requested_status_file(tmp_path):
    from ouroboros.tools.control import _schedule_task
    from ouroboros.task_results import STATUS_REQUESTED

    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        drive_root=tmp_path,
        is_direct_chat=False,
    )

    result = _schedule_task(ctx, "Do the thing", context="Model focus A")

    assert "Task request queued" in result
    task_id = ctx.pending_events[0]["task_id"]
    path = tmp_path / "task_results" / f"{task_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_REQUESTED
    assert data["description"] == "Do the thing"
    assert data["context"] == "Model focus A"


def test_get_task_result_returns_full_completed_output(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.control import _get_task_result

    full_text = ("hello\n" * 1200) + "TAIL_MARKER"
    write_task_result(
        tmp_path,
        "abc123",
        STATUS_COMPLETED,
        result=full_text,
        cost_usd=1.23,
        trace_summary="trace",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _get_task_result(ctx, "abc123")

    assert "TAIL_MARKER" in output
    assert full_text in output
    assert "[BEGIN_SUBTASK_OUTPUT]" in output


def test_wait_for_task_reports_rejected_duplicate(tmp_path):
    from ouroboros.task_results import STATUS_REJECTED_DUPLICATE, write_task_result
    from ouroboros.tools.control import _wait_for_task

    write_task_result(
        tmp_path,
        "dup123",
        STATUS_REJECTED_DUPLICATE,
        duplicate_of="orig999",
        result="Task was rejected as semantically similar to already active task orig999.",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _wait_for_task(ctx, "dup123")

    assert "rejected_duplicate" in output
    assert "duplicate_of=orig999" in output


def test_handle_schedule_task_duplicate_writes_rejected_status(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_REJECTED_DUPLICATE

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: "orig111")

    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "dup222",
            "description": "Do the thing",
            "context": "Model focus B",
            "depth": 1,
        },
        FakeCtx(),
    )

    path = tmp_path / "task_results" / "dup222.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_REJECTED_DUPLICATE
    assert data["duplicate_of"] == "orig111"
    assert sent and "Task rejected" in sent[0][1]


def test_handle_schedule_task_idempotent_by_task_id(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from supervisor import queue as q_module

    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "false")
    pending = []
    running = {}
    seq = {"value": 0}
    q_module.init(tmp_path, soft_timeout=60, hard_timeout=120)
    q_module.init_queue_refs(pending, running, seq)

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = pending
        RUNNING = running

        def load_state(self):
            return {"owner_chat_id": 1}

        def save_state(self, st):
            return None

        def send_with_budget(self, chat_id, text, **kwargs):
            return None

        def enqueue_task(self, task):
            return q_module.enqueue_task(task)

        def persist_queue_snapshot(self, reason=""):
            return None

    evt = {
        "type": "schedule_task",
        "task_id": "idem001",
        "description": "Do the thing once",
        "depth": 1,
    }
    ev_module._handle_schedule_task(evt, FakeCtx())
    ev_module._handle_schedule_task(evt, FakeCtx())

    assert len(pending) == 1
    assert pending[0]["id"] == "idem001"


def test_handle_schedule_task_allows_requested_status_seed(tmp_path):
    from supervisor import events as ev_module
    from supervisor import queue as q_module
    from ouroboros.task_results import STATUS_REQUESTED, write_task_result

    pending = []
    running = {}
    seq = {"value": 0}
    q_module.init(tmp_path, soft_timeout=60, hard_timeout=120)
    q_module.init_queue_refs(pending, running, seq)

    write_task_result(
        tmp_path,
        "seed001",
        STATUS_REQUESTED,
        description="Seeded requested status",
        result="Task request queued. Awaiting supervisor acceptance.",
    )

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = pending
        RUNNING = running

        def load_state(self):
            return {"owner_chat_id": 1}

        def save_state(self, st):
            return None

        def send_with_budget(self, chat_id, text, **kwargs):
            return None

        def enqueue_task(self, task):
            return q_module.enqueue_task(task)

        def persist_queue_snapshot(self, reason=""):
            return None

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "seed001",
            "description": "Seeded requested status",
            "depth": 1,
        },
        FakeCtx(),
    )

    assert len(pending) == 1
    assert pending[0]["id"] == "seed001"


def test_handle_text_response_keeps_full_reasoning_note():
    from ouroboros.loop import _handle_text_response

    content = "A" * 500
    llm_trace = {"reasoning_notes": [], "tool_calls": []}
    _, _, updated = _handle_text_response(content, llm_trace, {})

    assert updated["reasoning_notes"] == [content]


def test_handle_log_event_persists_status_transition(tmp_path):
    from supervisor import events as ev_module

    class _Bridge:
        def push_log(self, payload):
            return None

    class _Ctx:
        DRIVE_ROOT = tmp_path
        bridge = _Bridge()

        def append_jsonl(self, path, obj):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    ev_module._handle_log_event(
        {
            "type": "log_event",
            "data": {
                "type": "status_transition",
                "ts": "2026-03-30T00:00:00+00:00",
                "task_id": "task-1",
                "executor": "codex",
                "from_status": "scheduled",
                "to_status": "running",
            },
        },
        _Ctx(),
    )

    events_path = tmp_path / "logs" / "events.jsonl"
    lines = [ln.strip() for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["type"] == "status_transition"
    assert payload["to_status"] == "running"


def test_handle_log_event_does_not_persist_non_transition(tmp_path):
    from supervisor import events as ev_module

    class _Bridge:
        def push_log(self, payload):
            return None

    class _Ctx:
        DRIVE_ROOT = tmp_path
        bridge = _Bridge()

        def append_jsonl(self, path, obj):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    ev_module._handle_log_event(
        {
            "type": "log_event",
            "data": {
                "type": "executor_run",
                "ts": "2026-03-30T00:00:01+00:00",
                "task_id": "task-2",
                "executor": "claude_code",
                "status": "running",
            },
        },
        _Ctx(),
    )

    events_path = tmp_path / "logs" / "events.jsonl"
    assert not events_path.exists()
