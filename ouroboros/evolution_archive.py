"""Durable archive for evolution attempts."""

from __future__ import annotations

import json
import pathlib
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
