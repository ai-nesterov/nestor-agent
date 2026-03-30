"""Unit tests for _schedule_task in ouroboros/tools/control.py."""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from ouroboros.tools.control import _schedule_task
from ouroboros.tools.registry import ToolContext


class TestScheduleTask:
    """Tests for _schedule_task function."""

    @pytest.fixture
    def mock_ctx(self, tmp_path: Path) -> ToolContext:
        """Create a mock ToolContext for testing."""
        ctx = ToolContext(
            drive_root=tmp_path,
            repo_dir=tmp_path / "repo",
            task_id="test-parent-001",
            current_task_type="task",
            current_chat_id=123,
        )
        ctx.pending_events = []
        ctx.task_depth = 0
        ctx.is_direct_chat = False
        return ctx

    def test_valid_executor_ouroboros(self, mock_ctx: ToolContext):
        """Test that valid executor 'ouroboros' is accepted."""
        result = _schedule_task(mock_ctx, description="Test task", executor="ouroboros")
        
        assert "Task request queued" in result
        assert "ouroboros" in result
        assert len(mock_ctx.pending_events) == 1
        assert mock_ctx.pending_events[0]["type"] == "schedule_task"
        assert mock_ctx.pending_events[0]["executor"] == "ouroboros"

    def test_valid_executor_claude_code(self, mock_ctx: ToolContext):
        """Test that valid executor 'claude_code' is accepted."""
        result = _schedule_task(mock_ctx, description="Test task", executor="claude_code")
        
        assert "Task request queued" in result
        assert "claude_code" in result
        assert mock_ctx.pending_events[0]["executor"] == "claude_code"

    def test_valid_executor_codex(self, mock_ctx: ToolContext):
        """Test that valid executor 'codex' is accepted."""
        result = _schedule_task(mock_ctx, description="Test task", executor="codex")
        
        assert "Task request queued" in result
        assert "codex" in result
        assert mock_ctx.pending_events[0]["executor"] == "codex"

    def test_invalid_executor(self, mock_ctx: ToolContext):
        """Test that unknown executor returns error."""
        result = _schedule_task(mock_ctx, description="Test task", executor="unknown_executor")
        
        assert "ERROR" in result
        assert "Unknown executor" in result
        assert "unknown_executor" in result
        # Should not create event on validation error
        assert len(mock_ctx.pending_events) == 0

    def test_invalid_executor_case_insensitive(self, mock_ctx: ToolContext):
        """Test that executor validation is case-insensitive."""
        result = _schedule_task(mock_ctx, description="Test task", executor="INVALID")
        
        assert "ERROR" in result
        assert "Unknown executor" in result

    def test_valid_artifact_policy_patch_only(self, mock_ctx: ToolContext):
        """Test that valid artifact_policy 'patch_only' is accepted."""
        result = _schedule_task(mock_ctx, description="Test task", artifact_policy="patch_only")
        
        assert "Task request queued" in result
        assert mock_ctx.pending_events[0]["artifact_policy"] == "patch_only"

    def test_valid_artifact_policy_keep_worktree(self, mock_ctx: ToolContext):
        """Test that valid artifact_policy 'keep_worktree' is accepted."""
        result = _schedule_task(mock_ctx, description="Test task", artifact_policy="keep_worktree")
        
        assert "Task request queued" in result
        assert mock_ctx.pending_events[0]["artifact_policy"] == "keep_worktree"

    def test_invalid_artifact_policy(self, mock_ctx: ToolContext):
        """Test that unknown artifact_policy returns error."""
        result = _schedule_task(mock_ctx, description="Test task", artifact_policy="invalid_policy")
        
        assert "ERROR" in result
        assert "Unknown artifact_policy" in result
        assert "invalid_policy" in result
        assert len(mock_ctx.pending_events) == 0

    def test_task_id_generated(self, mock_ctx: ToolContext):
        """Test that task_id is generated and included in result."""
        result = _schedule_task(mock_ctx, description="Test task")
        
        # Result should contain task_id in format "Task request queued <task_id>"
        assert "Task request queued" in result
        
        # Extract task_id from result (format: "Task request queued abcdef12 (...): description")
        parts = result.split("Task request queued ")[1].split(" ")
        task_id = parts[0]
        
        # task_id should be 8 hex characters
        assert len(task_id) == 8
        assert all(c in "0123456789abcdef" for c in task_id)
        
        # Same task_id should be in pending_events
        assert mock_ctx.pending_events[0]["task_id"] == task_id

    def test_task_id_unique(self, mock_ctx: ToolContext):
        """Test that each call generates a unique task_id."""
        result1 = _schedule_task(mock_ctx, description="Task 1")
        result2 = _schedule_task(mock_ctx, description="Task 2")
        
        task_id1 = result1.split("Task request queued ")[1].split(" ")[0]
        task_id2 = result2.split("Task request queued ")[1].split(" ")[0]
        
        assert task_id1 != task_id2

    def test_description_included(self, mock_ctx: ToolContext):
        """Test that description is included in the result."""
        description = "My custom task description"
        result = _schedule_task(mock_ctx, description=description)
        
        assert description in result
        assert mock_ctx.pending_events[0]["description"] == description

    def test_depth_limit_exceeded(self, tmp_path: Path):
        """Test that subtask depth limit is enforced."""
        ctx = ToolContext(
            drive_root=tmp_path,
            repo_dir=tmp_path / "repo",
            task_id="test-parent",
            current_task_type="task",
            current_chat_id=123,
        )
        ctx.pending_events = []
        ctx.task_depth = 3  # Already at max depth
        ctx.is_direct_chat = False
        
        result = _schedule_task(ctx, description="Too deep task")
        
        assert "ERROR" in result
        assert "Subtask depth limit" in result
        assert "3" in result
        assert len(ctx.pending_events) == 0

    def test_default_executor_is_ouroboros(self, mock_ctx: ToolContext):
        """Test that default executor is 'ouroboros' when not specified."""
        result = _schedule_task(mock_ctx, description="Test task")
        
        assert "ouroboros" in result
        assert mock_ctx.pending_events[0]["executor"] == "ouroboros"

    def test_default_artifact_policy_is_patch_only(self, mock_ctx: ToolContext):
        """Test that default artifact_policy is 'patch_only' when not specified."""
        result = _schedule_task(mock_ctx, description="Test task")
        
        assert mock_ctx.pending_events[0]["artifact_policy"] == "patch_only"

    def test_context_and_parent_task_id(self, mock_ctx: ToolContext):
        """Test that context and parent_task_id are included in event."""
        context = "Some context for the task"
        parent_id = "parent-123"
        
        result = _schedule_task(
            mock_ctx,
            description="Test task",
            context=context,
            parent_task_id=parent_id,
        )
        
        assert "Task request queued" in result
        assert mock_ctx.pending_events[0].get("context") == context
        assert mock_ctx.pending_events[0].get("parent_task_id") == parent_id
