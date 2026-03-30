import json
import subprocess
from pathlib import Path

from ouroboros.executors.artifacts import ArtifactManager
from ouroboros.tools.executor_patches import _apply_task_patch
from ouroboros.tools.registry import ToolContext


def _run(cmd, cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "ouroboros"], repo)
    _run(["git", "config", "user.email", "tests@example.com"], repo)
    _run(["git", "config", "user.name", "Tests"], repo)
    (repo / "file.txt").write_text("line1\n", encoding="utf-8")
    _run(["git", "add", "file.txt"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    return repo


def test_artifact_manager_creates_required_layout(tmp_path):
    manager = ArtifactManager(tmp_path)
    artifact_dir = manager.prepare_artifact_dir("task123")

    for filename in manager.REQUIRED_FILES:
        assert (artifact_dir / filename).exists(), filename

    manager.write_manifest(artifact_dir, {"task_id": "task123", "base_sha": "abc"})
    loaded = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert loaded["task_id"] == "task123"


def test_apply_task_patch_rejects_dirty_repo(tmp_path):
    repo = _init_repo(tmp_path)
    drive = tmp_path / "data"
    ctx = ToolContext(repo_dir=repo, drive_root=drive)

    base_sha = _run(["git", "rev-parse", "HEAD"], repo)
    artifacts = ArtifactManager(drive)
    artifact_dir = artifacts.prepare_artifact_dir("t-dirty")
    artifacts.write_manifest(artifact_dir, {"task_id": "t-dirty", "base_sha": base_sha})
    artifacts.write_text(artifact_dir, "patch.diff", "")

    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    msg = _apply_task_patch(ctx, "t-dirty")
    assert "repo is dirty" in msg


def test_apply_task_patch_rejects_stale_base_sha(tmp_path):
    repo = _init_repo(tmp_path)
    drive = tmp_path / "data"
    ctx = ToolContext(repo_dir=repo, drive_root=drive)

    stale_base = _run(["git", "rev-parse", "HEAD"], repo)
    (repo / "file.txt").write_text("line1\nline2\n", encoding="utf-8")
    _run(["git", "add", "file.txt"], repo)
    _run(["git", "commit", "-m", "next"], repo)

    artifacts = ArtifactManager(drive)
    artifact_dir = artifacts.prepare_artifact_dir("t-stale")
    artifacts.write_manifest(artifact_dir, {"task_id": "t-stale", "base_sha": stale_base})
    artifacts.write_text(artifact_dir, "patch.diff", "diff --git a/file.txt b/file.txt\n")

    msg = _apply_task_patch(ctx, "t-stale")
    assert "base_sha mismatch" in msg
