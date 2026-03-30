from supervisor import events as ev_module
import datetime


class FakeCtx:
    def __init__(self, tmp_path, state):
        self.DRIVE_ROOT = tmp_path
        self.RUNNING = {}
        self._state = dict(state)
        self.enqueued = []
        self.sent = []

    def load_state(self):
        return dict(self._state)

    def save_state(self, st):
        self._state = dict(st)

    def send_with_budget(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    def enqueue_task(self, task):
        self.enqueued.append(task)

    def persist_queue_snapshot(self, reason=""):
        pass


def test_codex_daily_cap_blocks_enqueue(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_DAILY_TASK_CAP", "1")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    state = {"owner_chat_id": 1, "codex_runs_today": 1, "last_reset_at": today}
    ctx = FakeCtx(tmp_path, state)

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "q001",
            "description": "Implement parser",
            "depth": 1,
            "executor": "codex",
        },
        ctx,
    )

    assert not ctx.enqueued
    assert any("daily cap" in text.lower() for _, text in ctx.sent)


def test_codex_parallel_cap_blocks_enqueue(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_MAX_PARALLEL", "1")
    monkeypatch.setenv("CODEX_DAILY_TASK_CAP", "10")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    state = {"owner_chat_id": 1, "codex_runs_today": 0, "last_reset_at": today}
    ctx = FakeCtx(tmp_path, state)
    ctx.RUNNING = {
        "running1": {
            "task": {"id": "running1", "executor": "codex"}
        }
    }

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "q002",
            "description": "Implement parser",
            "depth": 1,
            "executor": "codex",
        },
        ctx,
    )

    assert not ctx.enqueued
    assert any("parallel capacity" in text.lower() for _, text in ctx.sent)
