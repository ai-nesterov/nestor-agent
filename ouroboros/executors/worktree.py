from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ouroboros.tools.registry import SAFETY_CRITICAL_PATHS


@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    executor: str
    branch: str
    base_branch: str
    base_sha: str
    path: Path


class WorktreeManager:
    def __init__(
        self,
        repo_dir: Path,
        branch_dev: str = "ouroboros",
        worktrees_root: Path | None = None,
        protected_paths: Iterable[str] | None = None,
    ):
        self.repo_dir = Path(repo_dir)
        self.branch_dev = branch_dev
        self.worktrees_root = Path(worktrees_root) if worktrees_root else (self.repo_dir.parent / "worktrees")
        self.protected_paths = set(protected_paths or SAFETY_CRITICAL_PATHS)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def prepare_worktree(self, task_id: str, base_branch: str | None, executor: str) -> WorktreeHandle:
        safe_task = str(task_id).strip()
        safe_executor = str(executor).strip().lower()
        source_branch = str(base_branch or self.branch_dev).strip()
        branch = f"task/{safe_task}/{safe_executor}"
        worktree_path = self.worktrees_root / f"{safe_task}-{safe_executor}"
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

        base_sha = self._git("rev-parse", source_branch)
        existing_branch = self._git("branch", "--list", branch)
        if existing_branch:
            self._git("branch", "-D", branch)
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        self._git("worktree", "add", "-b", branch, str(worktree_path), base_sha)
        return WorktreeHandle(
            task_id=safe_task,
            executor=safe_executor,
            branch=branch,
            base_branch=source_branch,
            base_sha=base_sha,
            path=worktree_path,
        )

    def cleanup_worktree(self, handle: WorktreeHandle, retain: bool = False) -> None:
        if retain:
            return
        try:
            self._git("worktree", "remove", "--force", str(handle.path))
        except Exception:
            pass
        try:
            self._git("branch", "-D", handle.branch)
        except Exception:
            pass
        if handle.path.exists():
            shutil.rmtree(handle.path, ignore_errors=True)

    def collect_patch(self, handle: WorktreeHandle, artifact_dir: Path) -> Path:
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        diff_proc = subprocess.run(
            ["git", "diff", "--binary"],
            cwd=str(handle.path),
            capture_output=True,
            text=True,
            check=True,
        )
        patch_path = artifact_dir / "patch.diff"
        patch_path.write_text(diff_proc.stdout, encoding="utf-8")

        changed_files = self._git("diff", "--name-only", cwd=handle.path).splitlines()
        (artifact_dir / "changed_files.json").write_text(
            json.dumps([f for f in changed_files if f], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        files = 0
        insertions = 0
        deletions = 0
        numstat = self._git("diff", "--numstat", cwd=handle.path)
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            if parts[0].isdigit():
                insertions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])

        (artifact_dir / "diff_stat.json").write_text(
            json.dumps(
                {"files": files, "insertions": insertions, "deletions": deletions},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return patch_path

    def assert_protected_paths_clean(self, handle: WorktreeHandle) -> None:
        changed = self._git("diff", "--name-only", cwd=handle.path).splitlines()
        changed_set = {p.strip() for p in changed if p.strip()}
        touched = sorted(self.protected_paths.intersection(changed_set))
        if touched:
            raise RuntimeError(f"Protected paths changed in worktree: {', '.join(touched)}")
