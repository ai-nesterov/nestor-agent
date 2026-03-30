import json
import subprocess
from pathlib import Path

from ouroboros.tools.executor_patches import _apply_task_patch, _validate_executor_result
from ouroboros.tools.registry import ToolContext


def _run(cmd, cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _run_raw_stdout(cmd, cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)
    return proc.stdout


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


def _mk_artifact_dir(drive: Path, task_id: str) -> Path:
    art = drive / "executor_runs" / task_id
    art.mkdir(parents=True, exist_ok=True)
    return art


def test_validate_executor_result_rejects_missing_artifacts(tmp_path):
    repo = _init_repo(tmp_path)
    drive = tmp_path / "data"
    ctx = ToolContext(repo_dir=repo, drive_root=drive)

    art = _mk_artifact_dir(drive, "t-missing")
    (art / "manifest.json").write_text("{}", encoding="utf-8")

    msg = _validate_executor_result(ctx, "t-missing")
    assert "validation failed" in msg.lower()
    assert "missing artifacts" in msg.lower()


def test_validate_executor_result_blocks_protected_path_patch(tmp_path):
    repo = _init_repo(tmp_path)
    drive = tmp_path / "data"
    ctx = ToolContext(repo_dir=repo, drive_root=drive)

    base_sha = _run(["git", "rev-parse", "HEAD"], repo)
    art = _mk_artifact_dir(drive, "t-protected")
    (art / "manifest.json").write_text(json.dumps({"task_id": "t-protected", "base_sha": base_sha}), encoding="utf-8")
    (art / "result.json").write_text("{}", encoding="utf-8")
    (art / "stdout.txt").write_text("", encoding="utf-8")
    (art / "stderr.txt").write_text("", encoding="utf-8")
    (art / "events.jsonl").write_text("", encoding="utf-8")
    (art / "changed_files.json").write_text("[]", encoding="utf-8")
    (art / "diff_stat.json").write_text("{}", encoding="utf-8")
    (art / "patch.diff").write_text(
        "diff --git a/BIBLE.md b/BIBLE.md\n--- a/BIBLE.md\n+++ b/BIBLE.md\n@@ -1 +1 @@\n-x\n+y\n",
        encoding="utf-8",
    )

    msg = _validate_executor_result(ctx, "t-protected")
    assert "protected paths" in msg.lower()


def test_apply_task_patch_success_after_validation(tmp_path):
    repo = _init_repo(tmp_path)
    drive = tmp_path / "data"
    ctx = ToolContext(repo_dir=repo, drive_root=drive)

    base_sha = _run(["git", "rev-parse", "HEAD"], repo)
    (repo / "file.txt").write_text("line1\nline2\n", encoding="utf-8")
    patch = _run_raw_stdout(["git", "diff", "--binary"], repo)
    _run(["git", "checkout", "--", "file.txt"], repo)

    art = _mk_artifact_dir(drive, "t-ok")
    (art / "manifest.json").write_text(json.dumps({"task_id": "t-ok", "base_sha": base_sha}), encoding="utf-8")
    (art / "result.json").write_text("{}", encoding="utf-8")
    (art / "stdout.txt").write_text("", encoding="utf-8")
    (art / "stderr.txt").write_text("", encoding="utf-8")
    (art / "events.jsonl").write_text("", encoding="utf-8")
    (art / "changed_files.json").write_text("[\"file.txt\"]", encoding="utf-8")
    (art / "diff_stat.json").write_text("{\"files\":1,\"insertions\":1,\"deletions\":0}", encoding="utf-8")
    (art / "patch.diff").write_text(patch, encoding="utf-8")

    validate_msg = _validate_executor_result(ctx, "t-ok")
    assert validate_msg.startswith("OK:")

    apply_msg = _apply_task_patch(ctx, "t-ok")
    assert apply_msg.startswith("OK:")
    assert "line2" in (repo / "file.txt").read_text(encoding="utf-8")
