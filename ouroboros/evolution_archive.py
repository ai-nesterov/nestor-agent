"""Durable archive for evolution attempts."""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from typing import Any, Dict, List

from ouroboros.utils import utc_now_iso


def evolution_archive_path(drive_root: Any) -> pathlib.Path:
    path = pathlib.Path(drive_root) / "state" / "evolution_archive.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_evolution_archive_entry(drive_root: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
    path = evolution_archive_path(drive_root)
    payload = dict(entry or {})
    payload.setdefault("ts", utc_now_iso())
    payload.setdefault("ts_unix", path.stat().st_mtime if path.exists() else 0.0)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def collect_recent_evolution_archive(drive_root: Any, limit: int = 40) -> List[Dict[str, Any]]:
    path = evolution_archive_path(drive_root)
    if not path.exists():
        return []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines[-max(1, int(limit)):]:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def summarize_evolution_archive(drive_root: Any, limit: int = 200) -> Dict[str, Any]:
    rows = collect_recent_evolution_archive(drive_root, limit=limit)
    if not rows:
        return {
            "attempts": 0,
            "accepted": 0,
            "rejected": 0,
            "committed_candidates": 0,
            "failed": 0,
            "acceptance_rate": 0.0,
            "avg_cost_per_accepted": 0.0,
            "top_outcome_reasons": [],
            "top_subsystems": [],
        }

    attempts = 0
    accepted = 0
    rejected = 0
    committed_candidates = 0
    failed = 0
    accepted_cost = 0.0
    reason_counter: Counter[str] = Counter()
    subsystem_counter: Counter[str] = Counter()

    for row in rows:
        outcome = str(row.get("outcome_class") or "").strip().lower()
        reason = str(row.get("outcome_reason") or "").strip()
        subsystem = str(row.get("objective_subsystem") or "").strip()
        if outcome:
            attempts += 1
        if outcome == "accepted":
            accepted += 1
            accepted_cost += float(row.get("cost_usd") or 0.0)
        elif outcome == "rejected":
            rejected += 1
        elif outcome == "committed":
            committed_candidates += 1
        elif outcome == "failed":
            failed += 1
        if reason:
            reason_counter[reason] += 1
        if subsystem:
            subsystem_counter[subsystem] += 1

    return {
        "attempts": attempts,
        "accepted": accepted,
        "rejected": rejected,
        "committed_candidates": committed_candidates,
        "failed": failed,
        "acceptance_rate": round((accepted / attempts) if attempts > 0 else 0.0, 4),
        "avg_cost_per_accepted": round((accepted_cost / accepted) if accepted > 0 else 0.0, 6),
        "top_outcome_reasons": [
            {"reason": reason, "count": count}
            for reason, count in reason_counter.most_common(5)
        ],
        "top_subsystems": [
            {"subsystem": subsystem, "count": count}
            for subsystem, count in subsystem_counter.most_common(5)
        ],
    }
