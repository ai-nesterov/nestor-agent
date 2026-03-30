import json
from types import SimpleNamespace


def test_schedule_task_persists_executor_metadata(tmp_path):
    from ouroboros.tools.control import _schedule_task

    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        drive_root=tmp_path,
        is_direct_chat=False,
    )

    result = _schedule_task(
        ctx,
        "Implement parser",
        context="Need deterministic output",
        executor="codex",
        repo_scope=["ouroboros", "tests"],
        constraints={"allow_network": False, "require_structured_output": True},
        artifact_policy="patch_only",
        quota_class="expensive",
        priority=3,
        budget_decision="force_run",
    )

    assert "Task request queued" in result
    evt = ctx.pending_events[0]
    assert evt["executor"] == "codex"
    assert evt["repo_scope"] == ["ouroboros", "tests"]
    assert evt["constraints"]["require_structured_output"] is True
    assert evt["quota_class"] == "expensive"
    assert evt["priority"] == 3
    assert evt["budget_decision"] == "force_run"

    task_id = evt["task_id"]
    data = json.loads((tmp_path / "task_results" / f"{task_id}.json").read_text(encoding="utf-8"))
    assert data["executor"] == "codex"
    assert data["artifact_policy"] == "patch_only"
    assert data["budget_decision"] == "force_run"


def test_schedule_task_event_roundtrip_to_queue_snapshot(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from supervisor import queue as q_module

    monkeypatch.setenv("EXTERNAL_EXECUTORS_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_CODE_ENABLED", "true")

    pending = []
    running = {}
    seq = {"value": 0}

    q_module.init(tmp_path, soft_timeout=60, hard_timeout=120)
    q_module.init_queue_refs(pending, running, seq)
    q_module.QUEUE_SNAPSHOT_PATH = tmp_path / "state" / "queue_snapshot.json"

    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path

        def load_state(self):
            return {"owner_chat_id": 777}

        def save_state(self, st):
            return None

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

        def enqueue_task(self, task):
            return q_module.enqueue_task(task)

        def persist_queue_snapshot(self, reason=""):
            q_module.persist_queue_snapshot(reason=reason)

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "taskx001",
            "description": "Refactor queue routing",
            "context": "keep backward compatibility",
            "depth": 1,
            "executor": "claude_code",
            "repo_scope": ["supervisor", "ouroboros/tools"],
            "constraints": {"allow_network": False},
            "artifact_policy": "keep_worktree",
            "quota_class": "expensive",
            "priority": 4,
            "budget_decision": "auto",
        },
        FakeCtx(),
    )

    assert len(pending) == 1
    task = pending[0]
    assert task["executor"] == "claude_code"
    assert task["executor_mode"] == "external_cli"
    assert task["artifact_policy"] == "keep_worktree"
    assert task["quota_class"] == "expensive"
    assert task["budget_decision"] == "auto"

    snap = json.loads(q_module.QUEUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    snap_task = snap["pending"][0]["task"]
    assert snap_task["executor"] == "claude_code"
    assert snap_task["repo_scope"] == ["supervisor", "ouroboros/tools"]
    assert snap_task["constraints"]["allow_network"] is False
    assert snap_task["budget_decision"] == "auto"
    assert sent and sent[0][0] == 777


def test_duplicate_detection_distinguishes_executor():
    from supervisor import events as ev_module

    pending = [
        {
            "id": "a1",
            "type": "task",
            "executor": "codex",
            "parent_task_id": "p1",
            "description": "Implement API client",
            "context": "",
        }
    ]

    dup = ev_module._find_duplicate_task(
        "Implement API client",
        "",
        pending,
        {},
        task_type="task",
        executor="claude_code",
        parent_task_id="p1",
    )
    assert dup is None
