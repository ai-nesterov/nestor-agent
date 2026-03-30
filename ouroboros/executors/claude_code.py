from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.executors.result import ExecutorResult
from ouroboros.executors.worktree import WorktreeHandle
from ouroboros.tools.registry import SAFETY_CRITICAL_PATHS
from ouroboros.utils import utc_now_iso


@dataclass
class ClaudeRunOutput:
    returncode: int
    stdout: str
    stderr: str


class ClaudeCodeRunner:
    def __init__(
        self,
        model: str = "sonnet",
        auth_mode: str = "subscription_only",
        timeout_sec: int = 300,
        cli_bin: str = "claude",
        permission_mode: str = "acceptEdits",
        dangerously_skip_permissions: bool = False,
    ):
        self.model = model
        self.auth_mode = auth_mode
        self.timeout_sec = int(timeout_sec)
        self.cli_bin = cli_bin
        self.permission_mode = str(permission_mode or "acceptEdits")
        self.dangerously_skip_permissions = bool(dangerously_skip_permissions)

    def _auth_mode_allowed(self, env: Dict[str, str]) -> tuple[bool, str, Dict[str, str]]:
        anth_key = str(env.get("ANTHROPIC_API_KEY") or "").strip()
        adjusted = dict(env)

        if self.auth_mode == "subscription_only":
            if anth_key:
                return False, "subscription_only policy forbids ANTHROPIC_API_KEY presence", adjusted
            adjusted.pop("ANTHROPIC_API_KEY", None)
            return True, "subscription", adjusted

        if self.auth_mode == "api_only":
            if not anth_key:
                return False, "api_only policy requires ANTHROPIC_API_KEY", adjusted
            return True, "api", adjusted

        # auto mode
        return True, "api" if anth_key else "subscription", adjusted

    def _run_claude(self, prompt: str, work_dir: Path, env: Dict[str, str]) -> ClaudeRunOutput:
        if not shutil.which(self.cli_bin):
            raise FileNotFoundError(f"{self.cli_bin} not found in PATH")

        cmd = [
            self.cli_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.model,
            "--permission-mode",
            self.permission_mode,
        ]
        if self.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=self.timeout_sec,
            env=env,
        )
        return ClaudeRunOutput(returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")

    def _parse_payload(self, stdout: str) -> Dict[str, Any]:
        text = str(stdout or "").strip()
        if not text:
            return {}
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for candidate in reversed(lines):
            if candidate.startswith("{") and candidate.endswith("}"):
                try:
                    data = json.loads(candidate)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    continue
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {}

    def _git(self, work_dir: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def _collect_diff(self, work_dir: Path) -> tuple[List[str], Dict[str, int]]:
        subprocess.run(
            ["git", "add", "-N", "--all"],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        changed = [x for x in self._git(work_dir, "diff", "--name-only").splitlines() if x.strip()]
        numstat = self._git(work_dir, "diff", "--numstat")
        files = 0
        ins = 0
        dele = 0
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            if parts[0].isdigit():
                ins += int(parts[0])
            if parts[1].isdigit():
                dele += int(parts[1])
        return changed, {"files": files, "insertions": ins, "deletions": dele}

    def _rollback_protected_paths(self, work_dir: Path, changed_files: List[str]) -> List[str]:
        touched = [p for p in changed_files if p in SAFETY_CRITICAL_PATHS]
        for path in touched:
            subprocess.run(["git", "checkout", "--", path], cwd=str(work_dir), check=False)
        return touched

    def run(self, task: dict, worktree: WorktreeHandle, artifact_dir: Path) -> ExecutorResult:
        started_at = utc_now_iso()
        task_id = str(task.get("id") or worktree.task_id)
        prompt = str(task.get("description") or task.get("text") or "").strip()
        context = str(task.get("context") or "").strip()
        if context:
            prompt = f"{prompt}\n\nContext:\n{context}"

        env = os.environ.copy()
        allowed, auth_message, env = self._auth_mode_allowed(env)
        if not allowed:
            finished_at = utc_now_iso()
            return ExecutorResult(
                task_id=task_id,
                executor="claude_code",
                status="failed",
                summary=f"Claude auth policy violation: {auth_message}",
                result_text="",
                artifact_dir=str(artifact_dir),
                base_sha=worktree.base_sha,
                worktree_path=str(worktree.path),
                usage={"auth_mode": "unknown", "usage_kind": "unknown", "cost_usd": None, "model": self.model},
                timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": 0},
            )

        try:
            run_out = self._run_claude(prompt=prompt, work_dir=worktree.path, env=env)
        except subprocess.TimeoutExpired:
            finished_at = utc_now_iso()
            return ExecutorResult(
                task_id=task_id,
                executor="claude_code",
                status="timeout",
                summary=f"Claude run timed out after {self.timeout_sec}s",
                result_text="",
                artifact_dir=str(artifact_dir),
                base_sha=worktree.base_sha,
                worktree_path=str(worktree.path),
                usage={"auth_mode": auth_message, "usage_kind": "unknown", "cost_usd": None, "model": self.model},
                timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": self.timeout_sec},
            )
        except Exception as exc:
            finished_at = utc_now_iso()
            return ExecutorResult(
                task_id=task_id,
                executor="claude_code",
                status="failed",
                summary=f"Claude run failed: {type(exc).__name__}: {exc}",
                result_text="",
                artifact_dir=str(artifact_dir),
                base_sha=worktree.base_sha,
                worktree_path=str(worktree.path),
                usage={"auth_mode": auth_message, "usage_kind": "unknown", "cost_usd": None, "model": self.model},
                timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": 0},
            )

        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "stdout.txt").write_text(run_out.stdout, encoding="utf-8")
        (artifact_dir / "stderr.txt").write_text(run_out.stderr, encoding="utf-8")
        (artifact_dir / "events.jsonl").write_text("", encoding="utf-8")

        payload = self._parse_payload(run_out.stdout)
        result_text = str(payload.get("result") or run_out.stdout).strip()
        cost = payload.get("total_cost_usd") if isinstance(payload.get("total_cost_usd"), (int, float)) else None

        changed_files, diff_stat = self._collect_diff(worktree.path)
        touched_protected = self._rollback_protected_paths(worktree.path, changed_files)
        if touched_protected:
            changed_files, diff_stat = self._collect_diff(worktree.path)

        status = "completed" if run_out.returncode == 0 and not touched_protected else "failed"
        if status == "completed" and not changed_files and "no-change" not in result_text.lower():
            status = "failed"
            result_text = f"{result_text}\n\nStop hook policy: no diff and no explicit no-change reason."

        summary = "Claude executor completed"
        if run_out.returncode != 0:
            summary = f"Claude CLI exited with code {run_out.returncode}"
        if touched_protected:
            summary = f"Blocked protected path changes: {', '.join(touched_protected)}"

        finished_at = utc_now_iso()
        return ExecutorResult(
            task_id=task_id,
            executor="claude_code",
            status=status,
            summary=summary,
            result_text=result_text,
            artifact_dir=str(artifact_dir),
            stdout_path=str(artifact_dir / "stdout.txt"),
            stderr_path=str(artifact_dir / "stderr.txt"),
            jsonl_path=str(artifact_dir / "events.jsonl"),
            changed_files=changed_files,
            diff_stat=diff_stat,
            usage={
                "auth_mode": auth_message,
                "usage_kind": "api_cost" if auth_message == "api" else "subscription_quota",
                "cost_usd": cost,
                "model": self.model,
            },
            base_sha=worktree.base_sha,
            worktree_path=str(worktree.path),
            external_session_id=str(payload.get("session_id") or "") or None,
            timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": max(0, self.timeout_sec if status == "timeout" else 0)},
        )
