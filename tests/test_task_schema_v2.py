import json
from types import SimpleNamespace


def test_get_task_result_backward_compatible_without_executor_fields(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.control import _get_task_result

    write_task_result(
        tmp_path,
        "legacy1",
        STATUS_COMPLETED,
        result="legacy-result",
        cost_usd=0.0,
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    text = _get_task_result(ctx, "legacy1")
    assert "legacy-result" in text
    assert "completed" in text


def test_restore_pending_from_legacy_snapshot_without_executor_fields(tmp_path):
    from supervisor import queue as q_module

    pending = []
    running = {}
    seq = {"value": 0}

    q_module.init(tmp_path, soft_timeout=30, hard_timeout=60)
    q_module.init_queue_refs(pending, running, seq)
    q_module.QUEUE_SNAPSHOT_PATH = tmp_path / "state" / "queue_snapshot.json"

    snap = {
        "ts": "2026-03-30T10:00:00+00:00",
        "reason": "legacy",
        "pending_count": 1,
        "running_count": 0,
        "pending": [
            {
                "id": "old001",
                "type": "task",
                "task": {
                    "id": "old001",
                    "type": "task",
                    "chat_id": 55,
                    "text": "legacy payload",
                    "description": "legacy payload",
                    "depth": 1,
                },
            }
        ],
        "running": [],
    }
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    q_module.QUEUE_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    q_module.QUEUE_SNAPSHOT_PATH.write_text(
        json.dumps(snap, ensure_ascii=False), encoding="utf-8"
    )

    restored = q_module.restore_pending_from_snapshot(max_age_sec=999999)

    assert restored == 1
    assert pending[0]["id"] == "old001"
    assert pending[0].get("executor") is None
