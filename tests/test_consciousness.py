"""
Tests for BackgroundConsciousness._emit_progress.

Verifies events have the correct shape, reach the queue,
and respect pause / chat_id=None semantics.

Run: pytest tests/test_consciousness.py -v
"""

import json
import os
import pathlib
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestEmitProgress(unittest.TestCase):
    """Tests for BackgroundConsciousness._emit_progress."""

    def _make_consciousness(self, chat_id=42, event_queue=None):
        """Create a BackgroundConsciousness with mocked dependencies."""
        from ouroboros.consciousness import BackgroundConsciousness

        tmpdir = tempfile.mkdtemp()
        drive_root = pathlib.Path(tmpdir)
        (drive_root / "logs").mkdir(parents=True, exist_ok=True)
        repo_dir = pathlib.Path(tmpdir) / "repo"
        repo_dir.mkdir()

        eq = event_queue if event_queue is not None else queue.Queue()

        with patch.object(BackgroundConsciousness, '_build_registry', return_value=MagicMock()):
            bc = BackgroundConsciousness(
                drive_root=drive_root,
                repo_dir=repo_dir,
                event_queue=eq,
                owner_chat_id_fn=lambda: chat_id,
            )
        return bc, eq, drive_root

    def test_event_shape(self):
        """Event has type, chat_id, text, is_progress, ts."""
        bc, eq, _ = self._make_consciousness(chat_id=99)
        bc._emit_progress("thinking about things")
        evt = eq.get_nowait()

        self.assertEqual(evt["type"], "send_message")
        self.assertEqual(evt["chat_id"], 99)
        self.assertEqual(evt["text"], "💬 thinking about things")
        self.assertEqual(evt["format"], "markdown")
        self.assertTrue(evt["is_progress"])
        self.assertIn("ts", evt)

    def test_event_reaches_queue(self):
        """Event actually ends up in the queue (not silently dropped)."""
        bc, eq, _ = self._make_consciousness()
        bc._emit_progress("hello world")
        self.assertFalse(eq.empty())

    def test_empty_content_skipped(self):
        """Empty or whitespace-only content produces no event."""
        bc, eq, drive_root = self._make_consciousness()
        progress_path = drive_root / "logs" / "progress.jsonl"

        bc._emit_progress("")
        bc._emit_progress("   ")
        bc._emit_progress(None)

        self.assertTrue(eq.empty())
        # Also should not persist to file
        self.assertFalse(progress_path.exists())

    def test_chat_id_none_skips_queue_but_persists(self):
        """When chat_id is None, event is NOT queued but IS persisted."""
        bc, eq, drive_root = self._make_consciousness(chat_id=None)
        bc._emit_progress("background thought")

        # Queue should be empty
        self.assertTrue(eq.empty())

        # File should have the entry
        progress_path = drive_root / "logs" / "progress.jsonl"
        self.assertTrue(progress_path.exists())
        entry = json.loads(progress_path.read_text().strip())
        self.assertEqual(entry["type"], "send_message")
        self.assertEqual(entry["content"], "background thought")
        self.assertTrue(entry["is_progress"])

    def test_paused_events_go_to_deferred(self):
        """When paused, events go to _deferred_events, not the queue."""
        bc, eq, _ = self._make_consciousness()
        bc._paused = True
        bc._emit_progress("deferred thought")

        self.assertTrue(eq.empty())
        self.assertEqual(len(bc._deferred_events), 1)
        self.assertEqual(bc._deferred_events[0]["type"], "send_message")
        self.assertEqual(bc._deferred_events[0]["text"], "💬 deferred thought")

    def test_think_skips_when_minimax_quota_is_soft_limited(self):
        bc, eq, drive_root = self._make_consciousness()
        bc._max_bg_rounds = 1
        bc._build_context = MagicMock(return_value="ctx")
        bc._tool_schemas = MagicMock(return_value=[])
        bc._llm.chat = MagicMock(side_effect=AssertionError("chat should not be called"))
        bc._llm.cloud_provider = MagicMock(return_value="minimax")

        with patch("ouroboros.consciousness.use_local_for_lane", return_value=False), \
             patch("ouroboros.consciousness.get_provider_quota_status", return_value={
                 "hard_blocked": False,
                 "soft_limited": True,
                 "reason": "MiniMax quota low",
             }):
            bc._think()

        events_path = drive_root / "logs" / "events.jsonl"
        self.assertTrue(events_path.exists())
        content = events_path.read_text(encoding="utf-8")
        self.assertIn("consciousness_quota_deferred", content)

    def test_think_emits_reasoning_to_progress_and_preview(self):
        bc, eq, drive_root = self._make_consciousness()
        bc._max_bg_rounds = 1
        bc._build_context = MagicMock(return_value="ctx")
        bc._tool_schemas = MagicMock(return_value=[])
        bc._llm.chat = MagicMock(return_value=(
            {"content": "visible answer", "reasoning": "private thought", "tool_calls": []},
            {"provider": "minimax", "cost": 0.0},
        ))
        bc._llm.cloud_provider = MagicMock(return_value="minimax")

        with patch("ouroboros.consciousness.use_local_for_lane", return_value=False), \
             patch("ouroboros.consciousness.get_provider_quota_status", return_value={
                 "hard_blocked": False,
                 "soft_limited": False,
                 "reason": "",
             }):
            bc._think()

        queued = []
        while not eq.empty():
            queued.append(eq.get_nowait())
        progress = next(evt for evt in queued if evt.get("type") == "send_message")
        self.assertEqual(progress["text"], "💬 private thought")

        events_path = drive_root / "logs" / "events.jsonl"
        content = events_path.read_text(encoding="utf-8")
        self.assertIn('"thought_preview": "private thought"', content)


@pytest.mark.slow
class TestLoopShutdownRace(unittest.TestCase):
    """Regression test: _loop must not raise 'cannot schedule new futures after
    shutdown' when the executor is shut down from within _think().

    The original bug: threading.Event.wait(timeout=N) was called directly in
    the loop thread while loop.run_until_complete() was active. When _think()
    called _tool_executor.shutdown(), the blocking wait() raced with the
    asyncio cancellation inside the same thread, raising:
        RuntimeError('cannot schedule new futures after shutdown')

    The fix: Event.wait() is now run in a background thread via
    loop.run_in_executor(), keeping it fully outside the run_until_complete()
    call stack so executor shutdown is safe at any point.
    """

    def _make_consciousness(self, chat_id=42, event_queue=None):
        """Create a BackgroundConsciousness with mocked dependencies."""
        from ouroboros.consciousness import BackgroundConsciousness

        tmpdir = tempfile.mkdtemp()
        drive_root = pathlib.Path(tmpdir)
        (drive_root / "logs").mkdir(parents=True, exist_ok=True)
        repo_dir = pathlib.Path(tmpdir) / "repo"
        repo_dir.mkdir()

        eq = event_queue if event_queue is not None else queue.Queue()

        with patch.object(BackgroundConsciousness, '_build_registry', return_value=MagicMock()):
            bc = BackgroundConsciousness(
                drive_root=drive_root,
                repo_dir=repo_dir,
                event_queue=eq,
                owner_chat_id_fn=lambda: chat_id,
            )
        return bc, eq, drive_root

    def test_loop_shutdown_no_runtime_error(self):
        """Stopping the loop while _think() runs must not raise
        'cannot schedule new futures after shutdown'.

        The bug: the old implementation called Event.wait(timeout) directly
        in the loop thread while inside loop.run_until_complete(). When
        _think() called _tool_executor.shutdown(wait=True), the executor
        cancelled the Event.wait future, causing run_until_complete to raise:
            RuntimeError('cannot schedule new futures after shutdown')

        The fix: Event.wait() runs in run_in_executor() (background thread),
        and _think() is called OUTSIDE run_until_complete(). The executor
        shutdown can never race with the inner asyncio call stack.

        This test directly instantiates the loop thread (bypassing start())
        so we can trigger the stop race synchronously.
        """
        bc, eq, drive_root = self._make_consciousness()

        # Track whether _think was reached and whether shutdown was called
        think_called = []
        bc._think = MagicMock(side_effect=lambda: think_called.append(True))
        bc._check_budget = MagicMock(return_value=True)

        # Give the loop a very short wakeup so it cycles quickly
        bc._next_wakeup_sec = 0.05

        # Run _loop in a thread so it can be stopped from the main thread
        thread = threading.Thread(target=bc._loop, name="consciousness-test")
        thread.start()

        # Wait for _think to be called at least once (loop is running)
        for _ in range(50):
            if think_called:
                break
            time.sleep(0.05)

        self.assertTrue(think_called, "_think was never called — loop may not be running")

        # Now stop — this races with the next Event.wait timeout
        bc._stop_event.set()
        bc._wakeup_event.set()  # wake it immediately if it's sleeping
        thread.join(timeout=3.0)

        self.assertFalse(thread.is_alive(), "_loop did not exit after stop_event.set()")
        # Reaching here without RuntimeError = test passes


if __name__ == "__main__":
    unittest.main()
