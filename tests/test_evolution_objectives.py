import json


def test_build_evolution_task_text_uses_structured_objective():
    from supervisor import queue as q_module

    text = q_module.build_evolution_task_text(
        3,
        objective={
            "description": "Reduce tool errors in recent tasks.",
            "hypothesis": "Tightening input validation will reduce noisy failures.",
            "subsystem": "tooling",
            "acceptance_checks": ["Create a repo_commit", "Reduce tool errors in the next run"],
        },
    )

    assert "OBJECTIVE:" in text
    assert "evolution_implementer" in text
    assert "HYPOTHESIS:" in text
    assert "TARGET SUBSYSTEM:" in text
    assert "ACCEPTANCE:" in text
    assert "Reduce tool errors in recent tasks." in text


def test_build_evolution_plan_task_text_mentions_planner_role():
    from supervisor import queue as q_module

    text = q_module.build_evolution_plan_task_text(
        4,
        objective={
            "description": "Plan a concrete runtime improvement.",
            "hypothesis": "A targeted patch will reduce report-only outcomes.",
            "subsystem": "agent_loop",
            "acceptance_checks": ["Produce a concrete implementable plan"],
        },
    )

    assert "evolution_planner" in text
    assert "PLAN_SUMMARY:" in text
    assert "TARGET_FILES:" in text
    assert "VALIDATION:" in text


def test_select_next_objective_prefers_blocked_reason(tmp_path):
    from ouroboros.evolution_objectives import select_next_objective

    objective = select_next_objective(
        tmp_path,
        state={"evolution_blocked_reason": "provider_or_fallback_blocked"},
    )

    assert objective["source"] == "evolution_state"
    assert "provider_or_fallback_blocked" in objective["description"]


def test_append_evolution_archive_entry_persists_row(tmp_path):
    from ouroboros.evolution_archive import append_evolution_archive_entry, evolution_archive_path

    append_evolution_archive_entry(
        tmp_path,
        {
            "task_id": "ev123",
            "objective_id": "obj456",
            "objective_description": "Reduce report-only completions.",
            "outcome_class": "failed",
        },
    )

    path = evolution_archive_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[-1])
    assert payload["task_id"] == "ev123"
    assert payload["objective_id"] == "obj456"
    assert payload["objective_description"] == "Reduce report-only completions."


def test_build_evolution_verify_task_text_mentions_verifier_role():
    from supervisor import queue as q_module

    text = q_module.build_evolution_verify_task_text("cand123", objective={"description": "Verify candidate"})

    assert "evolution_verifier" in text
    assert "VERIFIER_DECISION: ACCEPTED" in text


def test_summarize_evolution_archive_counts_acceptance(tmp_path):
    from ouroboros.evolution_archive import append_evolution_archive_entry, summarize_evolution_archive

    append_evolution_archive_entry(tmp_path, {"outcome_class": "accepted", "cost_usd": 1.2, "objective_subsystem": "tooling"})
    append_evolution_archive_entry(tmp_path, {"outcome_class": "rejected", "cost_usd": 0.4, "objective_subsystem": "tooling"})
    append_evolution_archive_entry(tmp_path, {"outcome_class": "committed", "cost_usd": 0.2, "objective_subsystem": "runtime"})

    summary = summarize_evolution_archive(tmp_path)

    assert summary["attempts"] == 3
    assert summary["accepted"] == 1
    assert summary["rejected"] == 1
    assert summary["committed_candidates"] == 1
    assert summary["acceptance_rate"] > 0
