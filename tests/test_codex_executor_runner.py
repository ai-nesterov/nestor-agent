import os
import subprocess
from pathlib import Path

from ouroboros.executors.codex import CodexRunner
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


def _install_fake_codex(tmp_path: Path) -> Path:
    script = Path(__file__).resolve().parent / "stubs" / "fake_codex.py"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "codex"
    shim.write_text(f"#!/usr/bin/env bash\npython3 {script} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def test_codex_runner_parses_json_and_writes_output(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH','')}")

    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")
    handle = manager.prepare_worktree("codex-1", "ouroboros", "codex")

    monkeypatch.setenv("FAKE_CODEX_TOUCH_FILE", str(handle.path / "main.py"))
    runner = CodexRunner(model="gpt-5.4", auth_mode="auto", timeout_sec=30)

    artifact_dir = tmp_path / "artifacts"
    result = runner.run({"id": "codex-1", "description": "edit"}, handle, artifact_dir)

    assert result.status == "completed"
    assert (artifact_dir / "result.json").exists()
    assert (artifact_dir / "events.jsonl").exists()
    assert (handle.path / ".codex" / "config.toml").exists()

    manager.cleanup_worktree(handle)


def test_codex_runner_schema_mismatch_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH','')}")
    monkeypatch.setenv("FAKE_CODEX_SCHEMA_INVALID", "1")

    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")
    handle = manager.prepare_worktree("codex-2", "ouroboros", "codex")

    runner = CodexRunner(model="gpt-5.4", auth_mode="auto", timeout_sec=30)
    result = runner.run({"id": "codex-2", "description": "edit"}, handle, tmp_path / "artifacts2")

    assert result.status == "failed"
    assert "schema validation failed" in result.summary.lower()

    manager.cleanup_worktree(handle)


def test_codex_runner_auth_modes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH','')}")

    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")
    handle = manager.prepare_worktree("codex-3", "ouroboros", "codex")

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    api_only = CodexRunner(model="gpt-5.4", auth_mode="api_only", timeout_sec=30)
    result_api = api_only.run({"id": "codex-3", "description": "edit"}, handle, tmp_path / "artifacts3")
    assert result_api.status == "failed"

    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    subscription_only = CodexRunner(model="gpt-5.4", auth_mode="subscription_only", timeout_sec=30)
    result_sub = subscription_only.run({"id": "codex-3", "description": "edit"}, handle, tmp_path / "artifacts4")
    assert result_sub.status == "failed"

    manager.cleanup_worktree(handle)
