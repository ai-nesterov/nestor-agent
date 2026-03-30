from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.executors.result import ExecutorResult
from ouroboros.executors.worktree import WorktreeHandle
from ouroboros.tools.registry import SAFETY_CRITICAL_PATHS
from ouroboros.utils import utc_now_iso


class CodexRunner:
    REQUIRED_FIELDS = {
        "summary": str,
        "tests_run": list,
        "tests_passed": bool,
        "changed_files": list,
        "risk_summary": str,
        "made_changes": bool,
    }

    def __init__(
        self,
        model: str = "gpt-5.4",
        auth_mode: str = "subscription_only",
        timeout_sec: int = 300,
        cli_bin: str = "codex",
        sandbox_mode: str = "workspace-write",
        approval_policy: str = "on-request",
        full_auto: bool = True,
        dangerously_bypass_approvals_and_sandbox: bool = False,
    ):
        self.model = model
        self.auth_mode = auth_mode
        self.timeout_sec = int(timeout_sec)
        self.cli_bin = cli_bin
        self.sandbox_mode = sandbox_mode
        self.approval_policy = approval_policy
        self.full_auto = bool(full_auto)
        self.dangerously_bypass_approvals_and_sandbox = bool(dangerously_bypass_approvals_and_sandbox)

    def _git(self, work_dir: Path, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=str(work_dir), capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def _collect_diff(self, work_dir: Path) -> tuple[List[str], Dict[str, int]]:
        subprocess.run(
            ["git", "add", "-N", "--all"],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        # Compare against HEAD so both staged and unstaged edits are counted.
        changed = [x for x in self._git(work_dir, "diff", "--name-only", "HEAD").splitlines() if x.strip()]
        numstat = self._git(work_dir, "diff", "--numstat", "HEAD")
        files = ins = dele = 0
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

    def _ensure_auth(self, env: Dict[str, str]) -> tuple[bool, str, str]:
        key = str(env.get("CODEX_API_KEY") or "").strip()
        if self.auth_mode == "subscription_only":
            if key:
                return False, "unknown", "subscription_only policy forbids CODEX_API_KEY fallback"
            return True, "subscription", ""
        if self.auth_mode == "api_only":
            if not key:
                return False, "unknown", "api_only policy requires CODEX_API_KEY"
            return True, "api", ""
        return True, "api" if key else "subscription", ""

    def _write_project_config(self, worktree_path: Path) -> Path:
        cfg_dir = worktree_path / ".codex"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = cfg_dir / "config.toml"
        cfg.write_text(
            "\n".join(
                [
                    f'model = "{self.model}"',
                    f'approval_policy = "{self.approval_policy}"',
                    f'sandbox_mode = "{self.sandbox_mode}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return cfg

    def _write_output_schema(self, artifact_dir: Path) -> Path:
        schema = {
            "type": "object",
            "required": list(self.REQUIRED_FIELDS.keys()),
            "properties": {
                "summary": {"type": "string"},
                "tests_run": {"type": "array", "items": {"type": "string"}},
                "tests_passed": {"type": "boolean"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "risk_summary": {"type": "string"},
                "made_changes": {"type": "boolean"},
            },
            "additionalProperties": False,
        }
        path = artifact_dir / "output_schema.json"
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _validate_final_output(self, payload: Dict[str, Any]) -> tuple[bool, str]:
        for key, expected_type in self.REQUIRED_FIELDS.items():
            if key not in payload:
                return False, f"missing required field '{key}'"
            if not isinstance(payload[key], expected_type):
                return False, f"field '{key}' has invalid type"
        extra = set(payload.keys()) - set(self.REQUIRED_FIELDS.keys())
        if extra:
            return False, f"unexpected fields: {sorted(extra)}"
        return True, ""

    def run(self, task: dict, worktree: WorktreeHandle, artifact_dir: Path) -> ExecutorResult:
        started_at = utc_now_iso()
        task_id = str(task.get("id") or worktree.task_id)
        prompt = str(task.get("description") or task.get("text") or "").strip()
        context = str(task.get("context") or "").strip()
        if context:
            prompt = f"{prompt}\n\nContext:\n{context}"

        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        final_out_path = artifact_dir / "result.json"
        jsonl_path = artifact_dir / "events.jsonl"
        stdout_path = artifact_dir / "stdout.txt"
        stderr_path = artifact_dir / "stderr.txt"

        env = os.environ.copy()
        ok, auth_mode, auth_error = self._ensure_auth(env)
        if not ok:
            finished_at = utc_now_iso()
            return ExecutorResult(
                task_id=task_id,
                executor="codex",
                status="failed",
                summary=f"Codex auth policy violation: {auth_error}",
                result_text="",
                artifact_dir=str(artifact_dir),
                base_sha=worktree.base_sha,
                worktree_path=str(worktree.path),
                usage={"auth_mode": auth_mode, "usage_kind": "unknown", "cost_usd": None, "model": self.model},
                timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": 0},
            )

        self._write_project_config(worktree.path)
        schema_path = self._write_output_schema(artifact_dir)

        if not shutil.which(self.cli_bin):
            finished_at = utc_now_iso()
            return ExecutorResult(
                task_id=task_id,
                executor="codex",
                status="failed",
                summary=f"{self.cli_bin} not found in PATH",
                result_text="",
                artifact_dir=str(artifact_dir),
                base_sha=worktree.base_sha,
                worktree_path=str(worktree.path),
                usage={"auth_mode": auth_mode, "usage_kind": "unknown", "cost_usd": None, "model": self.model},
                timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": 0},
            )

        cmd = [
            self.cli_bin,
            "exec",
            prompt,
            "--sandbox",
            self.sandbox_mode,
            "--json",
            "--output-schema",
            str(schema_path),
            "-o",
            str(final_out_path),
        ]
        if self.full_auto:
            cmd.append("--full-auto")
        if self.dangerously_bypass_approvals_and_sandbox:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(worktree.path),
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                env=env,
            )
        except subprocess.TimeoutExpired:
            finished_at = utc_now_iso()
            return ExecutorResult(
                task_id=task_id,
                executor="codex",
                status="timeout",
                summary=f"Codex run timed out after {self.timeout_sec}s",
                result_text="",
                artifact_dir=str(artifact_dir),
                base_sha=worktree.base_sha,
                worktree_path=str(worktree.path),
                usage={"auth_mode": auth_mode, "usage_kind": "unknown", "cost_usd": None, "model": self.model},
                timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": self.timeout_sec},
            )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        jsonl_path.write_text(stdout, encoding="utf-8")

        payload: Dict[str, Any] = {}
        if final_out_path.exists():
            try:
                payload = json.loads(final_out_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}

        valid, err = self._validate_final_output(payload)
        changed_files, diff_stat = self._collect_diff(worktree.path)
        touched_protected = self._rollback_protected_paths(worktree.path, changed_files)
        if touched_protected:
            changed_files, diff_stat = self._collect_diff(worktree.path)

        status = "completed"
        summary = "Codex executor completed"
        if proc.returncode != 0:
            status = "failed"
            summary = f"Codex CLI exited with code {proc.returncode}"
        if not valid:
            status = "failed"
            summary = f"Codex schema validation failed: {err}"
        if touched_protected:
            status = "failed"
            summary = f"Blocked protected path changes: {', '.join(touched_protected)}"

        result_text = json.dumps(payload, ensure_ascii=False, indent=2) if payload else stdout.strip()
        finished_at = utc_now_iso()

        return ExecutorResult(
            task_id=task_id,
            executor="codex",
            status=status,
            summary=summary,
            result_text=result_text,
            artifact_dir=str(artifact_dir),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            jsonl_path=str(jsonl_path),
            changed_files=changed_files,
            diff_stat=diff_stat,
            usage={
                "auth_mode": auth_mode,
                "usage_kind": "api_cost" if auth_mode == "api" else "subscription_quota",
                "cost_usd": None,
                "model": self.model,
            },
            base_sha=worktree.base_sha,
            worktree_path=str(worktree.path),
            timings={"started_at": started_at, "finished_at": finished_at, "duration_sec": 0},
        )
