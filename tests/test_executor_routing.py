from supervisor import events as ev_module


class FakeCtx:
    def __init__(self, tmp_path, owner_chat_id=1):
        self.DRIVE_ROOT = tmp_path
        self.RUNNING = {}
        self._state = {"owner_chat_id": owner_chat_id}
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


def test_executor_disabled_blocks_external_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "false")
    monkeypatch.setenv("CODEX_ENABLED", "true")

    ctx = FakeCtx(tmp_path)
    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "ext001",
            "description": "Implement feature",
            "depth": 1,
            "executor": "codex",
        },
        ctx,
    )

    assert not ctx.enqueued
    assert ctx.sent and "rejected" in ctx.sent[0][1].lower()


def test_ouroboros_task_still_schedules_when_external_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "false")

    ctx = FakeCtx(tmp_path)
    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "int001",
            "description": "Analyze logs",
            "depth": 1,
            "executor": "ouroboros",
        },
        ctx,
    )

    assert len(ctx.enqueued) == 1
    assert ctx.enqueued[0]["executor"] == "ouroboros"


def test_external_review_planning_task_can_be_scheduled_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_ALLOWED_IN_REVIEW", "true")

    ctx = FakeCtx(tmp_path)
    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "rev001",
            "description": "Plan review strategy",
            "depth": 1,
            "executor": "codex",
            "task_type": "review",
            "task_kind": "review_plan",
            "caller_class": "review",
        },
        ctx,
    )

    assert len(ctx.enqueued) == 1
    assert ctx.enqueued[0]["task_kind"] == "review_plan"
