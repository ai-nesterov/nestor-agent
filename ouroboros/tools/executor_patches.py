"""Explicit patch import/discard tools for external executor artifacts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry


def _artifact_dir(ctx: ToolContext, task_id: str) -> Path:
    return Path(ctx.drive_root) / "executor_runs" / str(task_id)


def _load_manifest(artifact_dir: Path) -> Dict[str, Any]:
    manifest_path = artifact_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest.json must contain a JSON object")
    return data


def _git(repo_dir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _is_repo_dirty(repo_dir: Path) -> bool:
    return bool(_git(repo_dir, "status", "--porcelain"))


def _summarize_working_tree(repo_dir: Path) -> Dict[str, int]:
    stat = _git(repo_dir, "diff", "--shortstat")
    files = insertions = deletions = 0
    if stat:
        import re

        files_m = re.search(r"(\d+) files? changed", stat)
        ins_m = re.search(r"(\d+) insertions?\(\+\)", stat)
        del_m = re.search(r"(\d+) deletions?\(-\)", stat)
        files = int(files_m.group(1)) if files_m else 0
        insertions = int(ins_m.group(1)) if ins_m else 0
        deletions = int(del_m.group(1)) if del_m else 0
    return {"files": files, "insertions": insertions, "deletions": deletions}


def _apply_task_patch(ctx: ToolContext, task_id: str, strategy: str = "apply") -> str:
    if strategy != "apply":
        return f"ERROR: Unsupported strategy '{strategy}'. Only 'apply' is supported in v1."

    repo_dir = Path(ctx.repo_dir)
    artifact_dir = _artifact_dir(ctx, task_id)
    if not artifact_dir.exists():
        return f"ERROR: artifact dir not found for task {task_id}: {artifact_dir}"

    patch_path = artifact_dir / "patch.diff"
    if not patch_path.exists():
        return f"ERROR: patch file missing for task {task_id}: {patch_path}"

    try:
        manifest = _load_manifest(artifact_dir)
    except Exception as exc:
        return f"ERROR: invalid manifest for task {task_id}: {exc}"

    base_sha = str(manifest.get("base_sha") or "").strip()
    if not base_sha:
        return "ERROR: manifest missing base_sha"

    if _is_repo_dirty(repo_dir):
        return "ERROR: main repo is dirty; commit/stash/discard local changes before patch import"

    current_sha = _git(repo_dir, "rev-parse", "HEAD")
    if current_sha != base_sha:
        return (
            "ERROR: base_sha mismatch; cannot apply stale patch "
            f"(manifest={base_sha[:12]}, current={current_sha[:12]})"
        )

    try:
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_path)],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return f"ERROR: git apply failed: {(exc.stderr or exc.stdout or str(exc)).strip()}"

    summary = _summarize_working_tree(repo_dir)
    return (
        f"OK: patch applied for task {task_id} "
        f"(files={summary['files']}, +{summary['insertions']}, -{summary['deletions']})"
    )


def _discard_task_patch(ctx: ToolContext, task_id: str) -> str:
    artifact_dir = _artifact_dir(ctx, task_id)
    if not artifact_dir.exists():
        return f"OK: no artifacts to discard for task {task_id}"
    shutil.rmtree(artifact_dir, ignore_errors=True)
    return f"OK: discarded executor artifacts for task {task_id}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "apply_task_patch",
            {
                "name": "apply_task_patch",
                "description": "Apply patch artifact from an external executor run into main repo (explicit import step, no commit).",
                "parameters": {
                    "type": "object",
                    "required": ["task_id"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "strategy": {"type": "string", "default": "apply"},
                    },
                },
            },
            _apply_task_patch,
        ),
        ToolEntry(
            "discard_task_patch",
            {
                "name": "discard_task_patch",
                "description": "Delete stored artifacts/patch for external executor task run.",
                "parameters": {
                    "type": "object",
                    "required": ["task_id"],
                    "properties": {"task_id": {"type": "string"}},
                },
            },
            _discard_task_patch,
        ),
    ]
