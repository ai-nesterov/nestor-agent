import json
from types import SimpleNamespace


def test_classify_outcome_from_facts_detects_commit():
    from ouroboros.outcome import classify_outcome_from_facts, default_execution_facts

    facts = default_execution_facts()
    facts["repo_commit_calls"] = 1

    outcome = classify_outcome_from_facts(
        task_type="task",
        execution_facts=facts,
        final_text="Committed the fix.",
    )

    assert outcome["outcome_class"] == "committed"
    assert outcome["productive"] is True


def test_apply_task_type_outcome_policy_fails_report_only_evolution():
    from ouroboros.outcome import apply_task_type_outcome_policy

    adjusted = apply_task_type_outcome_policy(
        task_type="evolution",
        execution_outcome={
            "outcome_class": "report_only",
            "outcome_reason": "text_only_completion_without_tool_execution",
            "outcome_source": "rule",
            "productive": False,
        },
    )

    assert adjusted["outcome_class"] == "failed"
    assert adjusted["outcome_reason"] == "evolution_requires_concrete_work_not_report_only"


def test_store_task_result_persists_canonical_outcome(tmp_path):
    from ouroboros.agent_task_pipeline import _store_task_result
    from ouroboros.task_results import load_task_result

    env = SimpleNamespace(drive_root=tmp_path)
    task = {"id": "task123", "description": "Do thing", "context": "ctx"}
    usage = {"cost": 0.0, "rounds": 2}
    llm_trace = {
        "reasoning_notes": [],
        "tool_calls": [],
        "execution_facts": {"tool_calls_total": 0, "repo_commit_calls": 0},
    }
    execution_outcome = {
        "outcome_class": "report_only",
        "outcome_reason": "text_only_completion_without_tool_execution",
        "outcome_source": "rule",
        "productive": False,
    }

    _store_task_result(env, task, "planned but not executed", usage, llm_trace, execution_outcome)

    payload = load_task_result(tmp_path, "task123")
    assert payload["outcome_class"] == "report_only"
    assert payload["execution_outcome"]["outcome_class"] == "report_only"
    assert payload["execution_facts"]["tool_calls_total"] == 0


def test_handle_task_done_prefers_canonical_outcome(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, load_task_result, write_task_result
    from supervisor import events as ev_module

    write_task_result(
        tmp_path,
        "canon001",
        STATUS_COMPLETED,
        outcome_class="executed_work",
        outcome_reason="mutating_tool_executed",
        outcome_source="rule",
        description="Do real work",
        caller_class="main_task_agent",
        result="done",
        trace_summary="## Tool trace (0 calls, 0 errors)\nNo tool calls.",
    )

    class _Bridge:
        def push_log(self, payload):
            return None

    class _Ctx:
        DRIVE_ROOT = tmp_path
        RUNNING = {}
        WORKERS = {}
        bridge = _Bridge()

        def load_state(self):
            return {}

        def save_state(self, st):
            return None

        def persist_queue_snapshot(self, reason=""):
            return None

        def append_jsonl(self, path, obj):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    ev_module._handle_task_done(
        {"type": "task_done", "task_id": "canon001", "task_type": "task"},
        _Ctx(),
    )

    payload = load_task_result(tmp_path, "canon001")
    assert payload["outcome_class"] == "executed_work"
    assert payload["outcome_source"] == "rule"
