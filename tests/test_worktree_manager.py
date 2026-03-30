import subprocess
from pathlib import Path

import pytest

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
    (repo / "app.txt").write_text("v1\n", encoding="utf-8")
    (repo / "BIBLE.md").write_text("safety\n", encoding="utf-8")
    _run(["git", "add", "app.txt", "BIBLE.md"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    return repo


def test_worktree_prepare_collect_and_cleanup(tmp_path):
    repo = _init_repo(tmp_path)
    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")

    handle = manager.prepare_worktree(task_id="t100", base_branch="ouroboros", executor="codex")
    assert handle.path.exists()
    assert handle.branch == "task/t100/codex"

    (handle.path / "app.txt").write_text("v1\nchange\n", encoding="utf-8")
    patch_path = manager.collect_patch(handle, tmp_path / "artifacts")

    assert patch_path.exists()
    assert "+change" in patch_path.read_text(encoding="utf-8")

    manager.cleanup_worktree(handle)
    assert not handle.path.exists()


def test_collect_patch_includes_untracked_files(tmp_path):
    repo = _init_repo(tmp_path)
    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")

    handle = manager.prepare_worktree(task_id="t-untracked", base_branch="ouroboros", executor="codex")
    (handle.path / "new_file.txt").write_text("hello\n", encoding="utf-8")

    patch_path = manager.collect_patch(handle, tmp_path / "artifacts")
    patch = patch_path.read_text(encoding="utf-8")

    assert "new_file.txt" in patch
    assert "+hello" in patch

    manager.cleanup_worktree(handle)


def test_collect_patch_includes_staged_only_changes(tmp_path):
    repo = _init_repo(tmp_path)
    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")

    handle = manager.prepare_worktree(task_id="t-staged", base_branch="ouroboros", executor="codex")
    target = handle.path / "app.txt"
    target.write_text("v2\n", encoding="utf-8")
    _run(["git", "add", "app.txt"], handle.path)

    patch_path = manager.collect_patch(handle, tmp_path / "artifacts")
    patch = patch_path.read_text(encoding="utf-8")
    changed = (tmp_path / "artifacts" / "changed_files.json").read_text(encoding="utf-8")

    assert "app.txt" in patch
    assert "app.txt" in changed

    manager.cleanup_worktree(handle)


def test_parallel_worktrees_do_not_conflict(tmp_path):
    repo = _init_repo(tmp_path)
    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")

    h1 = manager.prepare_worktree(task_id="a1", base_branch="ouroboros", executor="codex")
    h2 = manager.prepare_worktree(task_id="a2", base_branch="ouroboros", executor="claude_code")

    assert h1.path != h2.path
    assert h1.path.exists() and h2.path.exists()

    manager.cleanup_worktree(h1)
    manager.cleanup_worktree(h2)


def test_protected_paths_guard_detects_changes(tmp_path):
    repo = _init_repo(tmp_path)
    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")

    handle = manager.prepare_worktree(task_id="guard1", base_branch="ouroboros", executor="codex")
    (handle.path / "BIBLE.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        manager.assert_protected_paths_clean(handle)

    manager.cleanup_worktree(handle)


def test_protected_paths_guard_detects_untracked_changes(tmp_path):
    repo = _init_repo(tmp_path)
    manager = WorktreeManager(repo, branch_dev="ouroboros", worktrees_root=tmp_path / "worktrees")

    handle = manager.prepare_worktree(task_id="guard2", base_branch="ouroboros", executor="codex")
    (handle.path / "prompts").mkdir(parents=True, exist_ok=True)
    (handle.path / "prompts/SAFETY.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        manager.assert_protected_paths_clean(handle)

    manager.cleanup_worktree(handle)
