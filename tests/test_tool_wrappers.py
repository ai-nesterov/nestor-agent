from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from ouroboros.tools.control import _promote_to_stable, _request_restart
from ouroboros.tools.github import _create_issue
from ouroboros.tools.memory_tools import _memory_update_registry
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.telegram import telegram_send_message


def test_memory_update_registry_rejects_multiline_source_id(tmp_path):
    ctx = SimpleNamespace(drive_path=lambda rel: tmp_path / rel)
    result = _memory_update_registry(ctx, "foo\n### hacked", "- **Path:** x")
    assert "Invalid source_id" in result
    assert not (tmp_path / "memory" / "registry.md").exists()


def test_promote_to_stable_emits_to_event_queue(tmp_path):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.event_queue = Queue()

    result = _promote_to_stable(ctx, "ship it")

    assert "Promote to stable requested" in result
    assert ctx.pending_events == []
    evt = ctx.event_queue.get_nowait()
    assert evt["type"] == "promote_to_stable"
    assert evt["reason"] == "ship it"


def test_request_restart_emits_to_event_queue(tmp_path):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.event_queue = Queue()

    result = _request_restart(ctx, "reload")

    assert "Restart requested" in result
    assert ctx.pending_events == []
    evt = ctx.event_queue.get_nowait()
    assert evt["type"] == "restart_request"
    assert evt["reason"] == "reload"


def test_create_issue_reports_label_failure():
    ctx = SimpleNamespace(repo_dir=".")
    responses = iter(["https://github.com/o/r/issues/123", "⚠️ GH_ERROR: label missing"])

    with patch("ouroboros.tools.github._gh_cmd", side_effect=lambda *args, **kwargs: next(responses)):
        result = _create_issue(ctx, "test issue", labels="bad")

    assert "failed to add labels" in result
    assert "issues/123" in result
    assert "GH_ERROR" in result


def test_telegram_send_message_defaults_to_plain_text():
    captured = {}

    def fake_request(method, data=None):
        captured["method"] = method
        captured["data"] = data
        return {"ok": True}

    with patch("ouroboros.tools.telegram._is_enabled", return_value=True), \
            patch("ouroboros.tools.telegram._get_token", return_value="token"), \
            patch("ouroboros.tools.telegram._telegram_api_request", side_effect=fake_request):
        result = telegram_send_message("123", "hello _world_")

    assert "successfully" in result.lower()
    assert captured["method"] == "sendMessage"
    assert captured["data"]["chat_id"] == "123"
    assert captured["data"]["text"] == "hello _world_"
    assert "parse_mode" not in captured["data"]


def test_telegram_send_message_passes_explicit_parse_mode():
    captured = {}

    def fake_request(method, data=None):
        captured["data"] = data
        return {"ok": True}

    with patch("ouroboros.tools.telegram._is_enabled", return_value=True), \
            patch("ouroboros.tools.telegram._get_token", return_value="token"), \
            patch("ouroboros.tools.telegram._telegram_api_request", side_effect=fake_request):
        result = telegram_send_message("123", "*hello*", parse_mode="Markdown")

    assert "successfully" in result.lower()
    assert captured["data"]["parse_mode"] == "Markdown"
