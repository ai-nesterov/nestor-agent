import os
import subprocess
from pathlib import Path

from ouroboros.tools.registry import ToolContext
from ouroboros.tools import review as review_module


def _run(cmd, cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _install_fake_bin(tmp_path: Path, name: str, target_script: str) -> Path:
    script = Path(__file__).resolve().parent / "stubs" / target_script
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / name
    shim.write_text(f"#!/usr/bin/env bash\npython3 {script} \"$@\"\n", encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def _make_ctx_with_staged_change(tmp_path: Path) -> ToolContext:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    drive = tmp_path / "drive"
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    (drive / "locks").mkdir(parents=True, exist_ok=True)

    _run(["git", "init", "-b", "ouroboros"], repo)
    _run(["git", "config", "user.email", "tests@example.com"], repo)
    _run(["git", "config", "user.name", "Tests"], repo)
    (repo / "main.py").write_text("print('v1')\n", encoding="utf-8")
    (repo / "BIBLE.md").write_text("constitution\n", encoding="utf-8")
    _run(["git", "add", "main.py", "BIBLE.md"], repo)
    _run(["git", "commit", "-m", "init"], repo)

    (repo / "main.py").write_text("print('v2')\n", encoding="utf-8")
    _run(["git", "add", "main.py"], repo)
    return ToolContext(repo_dir=repo, drive_root=drive)


def test_codex_review_executor_runs_without_cloud(monkeypatch, tmp_path):
    bin_dir = _install_fake_bin(tmp_path, "codex", "fake_codex.py")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW",
        '[{"item":"code_quality","verdict":"PASS","severity":"critical","reason":"ok"}]',
    )

    ctx = _make_ctx_with_staged_change(tmp_path)
    result = review_module._run_unified_review(
        ctx,
        "test local codex review",
        review_executor="codex",
        repo_dir=ctx.repo_dir,
    )

    assert result is None
    assert ctx._review_advisory == []


def test_both_review_executor_requires_both_local_reviewers(monkeypatch, tmp_path):
    codex_bin = _install_fake_bin(tmp_path, "codex", "fake_codex.py")
    claude_bin = _install_fake_bin(tmp_path, "claude", "fake_claude.py")
    monkeypatch.setenv("PATH", f"{codex_bin}:{claude_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW",
        '[{"item":"code_quality","verdict":"PASS","severity":"critical","reason":"ok"}]',
    )
    monkeypatch.setenv(
        "FAKE_CLAUDE_RESULT",
        '[{"item":"tests","verdict":"PASS","severity":"advisory","reason":"ok"}]',
    )

    ctx = _make_ctx_with_staged_change(tmp_path)
    result = review_module._run_unified_review(
        ctx,
        "test both local reviewers",
        review_executor="both",
        repo_dir=ctx.repo_dir,
    )

    assert result is None


def test_local_review_no_write_enforcement_blocks_mutation(monkeypatch, tmp_path):
    bin_dir = _install_fake_bin(tmp_path, "claude", "fake_claude.py")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH_PROTECTED", "1")
    monkeypatch.setenv(
        "FAKE_CLAUDE_RESULT",
        '[{"item":"code_quality","verdict":"PASS","severity":"critical","reason":"ok"}]',
    )

    ctx = _make_ctx_with_staged_change(tmp_path)
    result = review_module._run_unified_review(
        ctx,
        "test claude review isolation",
        review_executor="claude_code",
        repo_dir=ctx.repo_dir,
    )

    assert result is not None
    assert "REVIEW_BLOCKED" in result
    assert "read-only" in result
