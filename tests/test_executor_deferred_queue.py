import datetime

from supervisor import events as ev_module


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


def test_high_importance_task_gets_deferred_when_quota_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_DAILY_TASK_CAP", "1")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    state = {"owner_chat_id": 1, "codex_runs_today": 1, "last_reset_at": today, "deferred_tasks": []}
    ctx = FakeCtx(tmp_path, state)

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "d001",
            "description": "Critical implementation",
            "depth": 1,
            "executor": "codex",
            "importance": "critical",
            "defer_on_quota": True,
            "budget_decision": "auto",
        },
        ctx,
    )

    assert not ctx.enqueued
    st = ctx.load_state()
    assert isinstance(st.get("deferred_tasks"), list)
    assert len(st["deferred_tasks"]) == 1
    assert st["deferred_tasks"][0]["task_id"] == "d001"
    assert st["deferred_tasks"][0]["budget_decision"] == "auto"


def test_force_run_bypasses_soft_budget_block(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_DAILY_TASK_CAP", "10")
    monkeypatch.setenv("CODEX_MAX_PARALLEL", "2")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    state = {"owner_chat_id": 1, "codex_runs_today": 9, "last_reset_at": today, "deferred_tasks": []}
    ctx = FakeCtx(tmp_path, state)

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "d003",
            "description": "Critical premium task",
            "depth": 1,
            "executor": "codex",
            "importance": "critical",
            "model_policy": "critical",
            "defer_on_quota": True,
            "budget_decision": "force_run",
        },
        ctx,
    )

    assert len(ctx.enqueued) == 1
    assert ctx.enqueued[0]["id"] == "d003"
    assert ctx.enqueued[0]["budget_decision"] == "force_run"


def test_budget_decision_defer_forces_deferral(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_DAILY_TASK_CAP", "10")
    monkeypatch.setenv("CODEX_MAX_PARALLEL", "2")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    state = {"owner_chat_id": 1, "codex_runs_today": 0, "last_reset_at": today, "deferred_tasks": []}
    ctx = FakeCtx(tmp_path, state)

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "d004",
            "description": "Defer by agent decision",
            "depth": 1,
            "executor": "codex",
            "importance": "low",
            "defer_on_quota": True,
            "budget_decision": "defer",
        },
        ctx,
    )

    assert not ctx.enqueued
    st = ctx.load_state()
    assert len(st["deferred_tasks"]) == 1
    assert st["deferred_tasks"][0]["task_id"] == "d004"
    assert st["deferred_tasks"][0]["deferred_reason"] == "budget decision requested deferral"


def test_resume_deferred_tasks_requeues_when_capacity_available(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CODEX_ENABLED", "true")
    monkeypatch.setenv("CODEX_DAILY_TASK_CAP", "10")
    monkeypatch.setenv("CODEX_MAX_PARALLEL", "2")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    state = {
        "owner_chat_id": 1,
        "codex_runs_today": 0,
        "last_reset_at": today,
        "deferred_tasks": [
            {
                "task_id": "d002",
                "description": "Resume me",
                "context": "",
                "executor": "codex",
                "task_type": "task",
                "task_kind": "implement",
                "caller_class": "main_task_agent",
                "model_policy": "balanced",
                "importance": "high",
                "repo_scope": [],
                "constraints": {},
                "artifact_policy": "patch_only",
                "quota_class": "cheap",
                "priority": 0,
                "depth": 1,
            }
        ],
    }
    ctx = FakeCtx(tmp_path, state)

    ev_module._handle_resume_deferred_tasks({"type": "resume_deferred_tasks", "limit": 10}, ctx)

    assert len(ctx.enqueued) == 1
    assert ctx.enqueued[0]["id"] == "d002"
    assert ctx.load_state().get("deferred_tasks") == []
