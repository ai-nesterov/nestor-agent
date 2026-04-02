"""Objective selection helpers for queued evolution tasks."""

from __future__ import annotations

import json
import pathlib
import time
import uuid
from typing import Any, Dict, List, Optional

from ouroboros.task_results import load_task_result


def _objective(
    *,
    source: str,
    subsystem: str,
    description: str,
    hypothesis: str,
    acceptance_checks: List[str],
    priority: int,
    evidence: Dict[str, Any] | None = None,
    cooldown_sec: int = 0,
) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:8],
        "source": source,
        "subsystem": subsystem,
        "description": description,
        "hypothesis": hypothesis,
        "acceptance_checks": list(acceptance_checks or []),
        "priority": int(priority),
        "evidence": dict(evidence or {}),
        "cooldown_sec": int(cooldown_sec),
    }


def _read_jsonl_tail(path: pathlib.Path, limit: int = 200) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def collect_objective_candidates(
    drive_root: Any,
    *,
    state: Dict[str, Any] | None = None,
    limit: int = 120,
) -> List[Dict[str, Any]]:
    root = pathlib.Path(drive_root)
    results_dir = root / "task_results"
    candidates: List[Dict[str, Any]] = []

    if isinstance(state, dict):
        blocked_reason = str(state.get("evolution_blocked_reason") or "").strip()
        if blocked_reason:
            candidates.append(_objective(
                source="evolution_state",
                subsystem="evolution_loop",
                description=f"Address repeated evolution blocker: {blocked_reason}",
                hypothesis="Resolving the most recent blocker will improve autonomous evolution throughput.",
                acceptance_checks=[
                    "Implement a concrete repository change",
                    "Create a repo_commit",
                    "Avoid repeating the same blocked reason on the next cycle",
                ],
                priority=95,
                evidence={"blocked_reason": blocked_reason},
                cooldown_sec=600,
            ))

    if results_dir.exists():
        result_files = sorted(
            results_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[: max(1, int(limit))]
        report_only = 0
        tool_error_tasks = 0
        review_failures = 0
        for path in result_files:
            payload = load_task_result(root, path.stem) or {}
            outcome_reason = str(payload.get("outcome_reason") or "").strip().lower()
            task_type = str(payload.get("task_type") or payload.get("type") or "").strip().lower()
            facts = payload.get("execution_facts") if isinstance(payload.get("execution_facts"), dict) else {}
            if outcome_reason in {
                "text_only_completion_without_tool_execution",
                "text_only_completion_requires_adjudication",
                "evolution_requires_concrete_work_not_report_only",
            }:
                report_only += 1
            if int(facts.get("tool_errors_total") or 0) > 0:
                tool_error_tasks += 1
            if task_type == "review" and str(payload.get("outcome_class") or "").strip().lower() == "failed":
                review_failures += 1

        if report_only > 0:
            candidates.append(_objective(
                source="task_results",
                subsystem="agent_loop",
                description="Reduce report-only completions in autonomous task execution.",
                hypothesis="Tightening prompts or tool-use logic should reduce narrative-only completions.",
                acceptance_checks=[
                    "Change runtime or prompt logic in the repository",
                    "Create a repo_commit",
                    "Do not end the task as report_only",
                ],
                priority=80 + min(report_only, 10),
                evidence={"report_only_count": report_only},
                cooldown_sec=1200,
            ))

        if tool_error_tasks > 0:
            candidates.append(_objective(
                source="task_results",
                subsystem="tooling",
                description="Reduce recurring tool execution errors in recent tasks.",
                hypothesis="Improving tool validation or execution handling should reduce retry waste and failures.",
                acceptance_checks=[
                    "Implement a repository change targeting a failing tool path",
                    "Create a repo_commit",
                    "Lower tool error frequency in the next similar task",
                ],
                priority=70 + min(tool_error_tasks, 10),
                evidence={"tool_error_tasks": tool_error_tasks},
                cooldown_sec=900,
            ))

        if review_failures > 0:
            candidates.append(_objective(
                source="task_results",
                subsystem="review_quality",
                description="Address failures surfaced by recent review tasks.",
                hypothesis="Fixing the highest-frequency review failure path will improve quality gates and reduce regressions.",
                acceptance_checks=[
                    "Implement a repository change guided by recent review failures",
                    "Create a repo_commit",
                    "Avoid repeating the same review failure pattern",
                ],
                priority=65 + min(review_failures, 10),
                evidence={"review_failures": review_failures},
                cooldown_sec=1800,
            ))

    events = _read_jsonl_tail(root / "logs" / "events.jsonl", limit=300)
    commit_test_failures = [
        evt for evt in events
        if str(evt.get("type") or "").strip().lower() == "commit_test_failure"
    ]
    if commit_test_failures:
        latest = commit_test_failures[-1]
        candidates.append(_objective(
            source="events",
            subsystem="test_gate",
            description="Fix the latest commit-time test failure encountered by the agent.",
            hypothesis="Repairing the failing test path will increase the probability that future mutations are accepted cleanly.",
            acceptance_checks=[
                "Target the failing test or its root cause",
                "Create a repo_commit",
                "Do not reproduce the same commit_test_failure event",
            ],
            priority=90,
            evidence={
                "last_test_failure": str(latest.get("commit_message") or "")[:200],
                "test_output": str(latest.get("test_output") or "")[:500],
                "consecutive_failures": int(latest.get("consecutive_failures") or 0),
            },
            cooldown_sec=1200,
        ))

    if not candidates:
        candidates.append(_objective(
            source="fallback",
            subsystem="runtime",
            description="Make one small, concrete improvement to the agent runtime with a clear payoff.",
            hypothesis="A narrowly scoped runtime improvement with validation is preferable to idle self-analysis.",
            acceptance_checks=[
                "Modify a repository file",
                "Create a repo_commit",
                "Explain the concrete improvement achieved",
            ],
            priority=10,
            evidence={},
            cooldown_sec=300,
        ))

    return sorted(candidates, key=lambda item: (-int(item.get("priority") or 0), str(item.get("description") or "")))


def _run_quick_test(drive_root: pathlib.Path, timeout_sec: int = 120) -> bool:
    """Return True if tests pass, False if tests fail or error."""
    import subprocess, threading

    repo_root = pathlib.Path.home() / "projects" / "nestor-agent"
    result = {"ok": False}

    def runner():
        try:
            proc = subprocess.run(
                ["python3", "-m", "pytest", "tests/", "-q", "--tb=no", "-x"],
                cwd=str(repo_root),
                capture_output=True,
                timeout=timeout_sec,
            )
            result["ok"] = proc.returncode == 0
        except Exception:
            result["ok"] = False

    t = threading.Thread(target=runner)
    t.daemon = True
    t.start()
    t.join(timeout=timeout_sec + 5)
    return result["ok"]


def select_next_objective(
    drive_root: Any,
    *,
    state: Dict[str, Any] | None = None,
    limit: int = 120,
) -> Optional[Dict[str, Any]]:
    candidates = collect_objective_candidates(drive_root, state=state, limit=limit)
    archive_path = pathlib.Path(drive_root) / "state" / "evolution_archive.jsonl"
    recent_rows = _read_jsonl_tail(archive_path, limit=80)
    latest_by_desc: Dict[str, float] = {}
    for row in recent_rows:
        desc = str(row.get("objective_description") or "").strip()
        if not desc:
            continue
        latest_by_desc[desc] = max(latest_by_desc.get(desc, 0.0), float(row.get("ts_unix") or 0.0))

    now = time.time()
    for candidate in candidates:
        desc = str(candidate.get("description") or "").strip()
        last_ts = latest_by_desc.get(desc, 0.0)
        cooldown_sec = int(candidate.get("cooldown_sec") or 0)
        if cooldown_sec > 0 and last_ts > 0 and (now - last_ts) < cooldown_sec:
            continue

        # Stale-evidence guard: verify test_gate failures are real before returning
        subsystem = str(candidate.get("subsystem") or "").strip().lower()
        if subsystem == "test_gate":
            root = pathlib.Path(drive_root)
            if _run_quick_test(root):
                continue  # tests pass — skip stale test_gate objective

        return candidate

    return None
