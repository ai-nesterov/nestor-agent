from ouroboros.tool_policy import (
    caller_can_schedule_external_executor,
    recommend_executor,
)


def test_recommend_executor_defaults_to_ouroboros_for_analysis():
    assert recommend_executor(task_type="task", analysis_only=True) == "ouroboros"


def test_recommend_executor_uses_claude_for_architecture_heavy():
    assert recommend_executor(task_type="task", architecture_heavy=True) == "claude_code"


def test_recommend_executor_uses_codex_for_deterministic_impl():
    assert recommend_executor(
        task_type="task",
        implementation_heavy=True,
        deterministic_output_required=True,
    ) == "codex"


def test_caller_class_restrictions_for_external_executors():
    assert caller_can_schedule_external_executor(caller_class="review", task_type="task") is False
    assert caller_can_schedule_external_executor(caller_class="consciousness", task_type="task") is False
    assert caller_can_schedule_external_executor(caller_class="main_task_agent", task_type="task") is True
