import datetime
import json


def _iso_ago(seconds: int) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    ).isoformat()


def test_reconcile_orphaned_scheduled_results_marks_failed(tmp_path):
    from ouroboros.task_results import STATUS_SCHEDULED, load_task_result, write_task_result
    from supervisor import queue as q_module

    pending = []
    running = {}
    seq = {"value": 0}
    q_module.init(tmp_path, soft_timeout=60, hard_timeout=120)
    q_module.init_queue_refs(pending, running, seq)

    write_task_result(
        tmp_path,
        "sched-orphan-1",
        STATUS_SCHEDULED,
        description="orphaned scheduled task",
        ts=_iso_ago(600),
    )

    fixed = q_module.reconcile_orphaned_scheduled_results(max_age_sec=120, scan_limit=50)
    assert fixed == 1

    payload = load_task_result(tmp_path, "sched-orphan-1")
    assert payload and payload.get("status") == "failed"
    assert "orphaned" in str(payload.get("result") or "").lower()


def test_reconcile_orphaned_scheduled_results_keeps_active_pending(tmp_path):
    from ouroboros.task_results import STATUS_SCHEDULED, load_task_result, write_task_result
    from supervisor import queue as q_module

    pending = []
    running = {}
    seq = {"value": 0}
    q_module.init(tmp_path, soft_timeout=60, hard_timeout=120)
    q_module.init_queue_refs(pending, running, seq)

    task_id = "sched-active-1"
    pending.append({"id": task_id, "type": "task", "executor": "codex"})

    write_task_result(
        tmp_path,
        task_id,
        STATUS_SCHEDULED,
        description="still pending",
        ts=_iso_ago(600),
    )

    fixed = q_module.reconcile_orphaned_scheduled_results(max_age_sec=120, scan_limit=50)
    assert fixed == 0

    payload = load_task_result(tmp_path, task_id)
    assert payload and payload.get("status") == "scheduled"

    log_path = tmp_path / "logs" / "supervisor.jsonl"
    if log_path.exists():
        rows = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert all(str(r.get("type")) != "orphaned_scheduled_task_failed" for r in rows)
