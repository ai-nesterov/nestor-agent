import os
import subprocess
from pathlib import Path

from ouroboros.executors.claude_code import ClaudeCodeRunner
from ouroboros.executors.worktree import WorktreeManager


def _run(cmd, cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "ouroboros"], repo)
    _run(["git", "config", "user.email", "tests@example.com"], repo)
    _run(["git", "config", "user.name", "Tests"], repo)
    (repo / "main.py").write_text("print('x')\n", encoding="utf-8")
    (repo / "BIBLE.md").write_text("safety\n", encoding="utf-8")
    _run(["git", "add", "main.py", "BIBLE.md"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    return repo


def _install_fake_claude(tmp_path: Path) -> Path:
    script = Path(__file__).resolve().parent / "stubs" / "fake_claude.py"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "claude"
    shim.write_text(f"#!/usr/bin/env bash\npython3 {script} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def test_runner_creates_artifacts_and_usage(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    bin_dir = _install_fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH','')}")

    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")
    handle = manager.prepare_worktree("t-run-1", "ouroboros", "claude_code")

    target = handle.path / "main.py"
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH_FILE", str(target))
    runner = ClaudeCodeRunner(model="sonnet", auth_mode="auto", timeout_sec=30)

    artifact_dir = tmp_path / "artifacts"
    result = runner.run({"id": "t-run-1", "description": "change file"}, handle, artifact_dir)

    assert result.status == "completed"
    assert "main.py" in result.changed_files
    assert (artifact_dir / "stdout.txt").exists()
    assert result.usage["auth_mode"] in {"subscription", "api"}

    manager.cleanup_worktree(handle)


def test_subscription_only_fails_with_api_key(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    bin_dir = _install_fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH','')}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")
    handle = manager.prepare_worktree("t-run-2", "ouroboros", "claude_code")

    runner = ClaudeCodeRunner(model="sonnet", auth_mode="subscription_only", timeout_sec=30)
    result = runner.run({"id": "t-run-2", "description": "noop"}, handle, tmp_path / "artifacts2")

    assert result.status == "failed"
    assert "auth policy violation" in result.summary.lower()

    manager.cleanup_worktree(handle)


def test_protected_path_changes_are_blocked(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    bin_dir = _install_fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH','')}")
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH_PROTECTED", "1")

    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")
    handle = manager.prepare_worktree("t-run-3", "ouroboros", "claude_code")

    runner = ClaudeCodeRunner(model="sonnet", auth_mode="auto", timeout_sec=30)
    result = runner.run({"id": "t-run-3", "description": "touch protected"}, handle, tmp_path / "artifacts3")

    assert result.status == "failed"
    assert "protected path" in result.summary.lower()

    manager.cleanup_worktree(handle)
