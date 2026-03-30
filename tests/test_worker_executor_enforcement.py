import json


class _DummyQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class _DummyProc:
    def is_alive(self):
        return True


def _init_state(tmp_path, owner_chat_id=1):
    from supervisor import state as st_module

    st_module.init(tmp_path, total_budget_limit=0.0)
    st = st_module.load_state()
    st["owner_chat_id"] = owner_chat_id
    st_module.save_state(st)


def test_assign_tasks_rejects_unroutable_external_executor(tmp_path, monkeypatch):
    from supervisor import workers
    from supervisor import queue as q_module

    _init_state(tmp_path, owner_chat_id=1)

    orig_drive = workers.DRIVE_ROOT
    orig_workers = dict(workers.WORKERS)
    orig_pending = list(workers.PENDING)
    orig_running = dict(workers.RUNNING)
    try:
        workers.DRIVE_ROOT = tmp_path
        workers.WORKERS.clear()
        workers.PENDING[:] = []
        workers.RUNNING.clear()

        sent = []
        monkeypatch.setattr(workers, "send_with_budget", lambda chat_id, text, **kwargs: sent.append((chat_id, text)))
        monkeypatch.setattr(q_module, "persist_queue_snapshot", lambda reason="": None)

        w = workers.Worker(wid=0, proc=_DummyProc(), in_q=_DummyQueue(), busy_task_id=None, kind="ouroboros")
        workers.WORKERS[0] = w

        task_id = "extfail01"
        workers.PENDING.append(
            {
                "id": task_id,
                "type": "task",
                "chat_id": 1,
                "description": "Must run on codex",
                "executor": "codex",
                "task_kind": "implement",
                "caller_class": "main_task_agent",
                "model_policy": "balanced",
                "importance": "medium",
                "defer_on_quota": True,
            }
        )

        workers.assign_tasks()

        assert workers.PENDING == []
        assert workers.RUNNING == {}
        assert w.in_q.items == []
        assert sent and "not routable" in sent[0][1].lower()

        result_path = tmp_path / "task_results" / f"{task_id}.json"
        assert result_path.exists()
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        assert payload.get("status") == "failed"
        assert "not routable" in str(payload.get("result") or "").lower()
    finally:
        workers.DRIVE_ROOT = orig_drive
        workers.WORKERS.clear()
        workers.WORKERS.update(orig_workers)
        workers.PENDING[:] = orig_pending
        workers.RUNNING.clear()
        workers.RUNNING.update(orig_running)


def test_assign_tasks_still_assigns_matching_executor(tmp_path, monkeypatch):
    from supervisor import workers
    from supervisor import queue as q_module

    _init_state(tmp_path, owner_chat_id=1)

    orig_drive = workers.DRIVE_ROOT
    orig_workers = dict(workers.WORKERS)
    orig_pending = list(workers.PENDING)
    orig_running = dict(workers.RUNNING)
    try:
        workers.DRIVE_ROOT = tmp_path
        workers.WORKERS.clear()
        workers.PENDING[:] = []
        workers.RUNNING.clear()

        monkeypatch.setattr(q_module, "persist_queue_snapshot", lambda reason="": None)

        w = workers.Worker(wid=0, proc=_DummyProc(), in_q=_DummyQueue(), busy_task_id=None, kind="ouroboros")
        workers.WORKERS[0] = w

        task_id = "okroute01"
        workers.PENDING.append(
            {
                "id": task_id,
                "type": "task",
                "chat_id": 1,
                "description": "Internal task",
                "executor": "ouroboros",
            }
        )

        workers.assign_tasks()

        assert workers.PENDING == []
        assert task_id in workers.RUNNING
        assert workers.RUNNING[task_id]["worker_id"] == 0
        assert len(w.in_q.items) == 1
        assert w.in_q.items[0]["id"] == task_id
    finally:
        workers.DRIVE_ROOT = orig_drive
        workers.WORKERS.clear()
        workers.WORKERS.update(orig_workers)
        workers.PENDING[:] = orig_pending
        workers.RUNNING.clear()
        workers.RUNNING.update(orig_running)


def test_assign_tasks_routes_codex_task_to_codex_worker(tmp_path, monkeypatch):
    from supervisor import workers
    from supervisor import queue as q_module

    _init_state(tmp_path, owner_chat_id=1)

    orig_drive = workers.DRIVE_ROOT
    orig_workers = dict(workers.WORKERS)
    orig_pending = list(workers.PENDING)
    orig_running = dict(workers.RUNNING)
    try:
        workers.DRIVE_ROOT = tmp_path
        workers.WORKERS.clear()
        workers.PENDING[:] = []
        workers.RUNNING.clear()

        monkeypatch.setattr(q_module, "persist_queue_snapshot", lambda reason="": None)

        w_main = workers.Worker(wid=0, proc=_DummyProc(), in_q=_DummyQueue(), busy_task_id=None, kind="ouroboros")
        w_codex = workers.Worker(wid=1, proc=_DummyProc(), in_q=_DummyQueue(), busy_task_id=None, kind="codex")
        workers.WORKERS[0] = w_main
        workers.WORKERS[1] = w_codex

        task_id = "codexr01"
        workers.PENDING.append(
            {
                "id": task_id,
                "type": "task",
                "chat_id": 1,
                "description": "Run externally",
                "executor": "codex",
            }
        )

        workers.assign_tasks()

        assert workers.PENDING == []
        assert task_id in workers.RUNNING
        assert workers.RUNNING[task_id]["worker_id"] == 1
        assert len(w_codex.in_q.items) == 1
        assert w_codex.in_q.items[0]["id"] == task_id
        assert w_main.in_q.items == []
    finally:
        workers.DRIVE_ROOT = orig_drive
        workers.WORKERS.clear()
        workers.WORKERS.update(orig_workers)
        workers.PENDING[:] = orig_pending
        workers.RUNNING.clear()
        workers.RUNNING.update(orig_running)
