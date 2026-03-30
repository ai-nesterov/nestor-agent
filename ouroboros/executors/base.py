from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .result import ExecutorResult
from .worktree import WorktreeHandle


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    kind: Literal["internal_agent", "external_cli"]
    supports_subscription_auth: bool
    supports_api_auth: bool
    default_timeout_sec: int


class ExternalExecutorRunner(Protocol):
    def run(self, task: dict, worktree: WorktreeHandle, artifact_dir: Path) -> ExecutorResult:
        ...
