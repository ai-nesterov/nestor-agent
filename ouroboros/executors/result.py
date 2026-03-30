from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal


@dataclass
class ExecutorResult:
    task_id: str
    executor: Literal["claude_code", "codex", "ouroboros"]
    status: Literal["completed", "failed", "timeout", "cancelled"]
    summary: str
    result_text: str = ""
    artifact_dir: str = ""
    patch_path: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    jsonl_path: str | None = None
    changed_files: List[str] = field(default_factory=list)
    diff_stat: Dict[str, int] = field(default_factory=lambda: {"files": 0, "insertions": 0, "deletions": 0})
    usage: Dict[str, Any] = field(
        default_factory=lambda: {
            "auth_mode": "unknown",
            "usage_kind": "unknown",
            "cost_usd": None,
            "model": None,
        }
    )
    base_sha: str = ""
    worktree_path: str | None = None
    worktree_retained: bool = False
    external_session_id: str | None = None
    timings: Dict[str, Any] = field(default_factory=dict)
    executor_result_schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "executor": self.executor,
            "status": self.status,
            "summary": self.summary,
            "result_text": self.result_text,
            "artifact_dir": self.artifact_dir,
            "patch_path": self.patch_path,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "jsonl_path": self.jsonl_path,
            "changed_files": list(self.changed_files),
            "diff_stat": dict(self.diff_stat),
            "usage": dict(self.usage),
            "base_sha": self.base_sha,
            "worktree_path": self.worktree_path,
            "worktree_retained": bool(self.worktree_retained),
            "external_session_id": self.external_session_id,
            "timings": dict(self.timings),
            "executor_result_schema_version": self.executor_result_schema_version,
        }
