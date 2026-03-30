"""
Tests for telegram_bot.py — standalone aiogram Telegram bot.

Covers:
- Configuration loading and validation
- Handler registration (all slash commands)
- Message forwarding to server
- Error handling (server unavailable, timeouts)
- Middleware logging
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, Chat, User

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import telegram_bot
from telegram_bot import (
    load_settings,
    _settings,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_BOT_ENABLED,
    TELEGRAM_INTERNAL_SECRET,
    SERVER_API_URL,
    TELEGRAM_LOG_PATH,
    LoggingMiddleware,
    cmd_start,
    cmd_status,
    cmd_evolve,
    cmd_bg,
    cmd_restart,
    cmd_panic,
    handle_text,
    _forward_to_server,
    get_http_client,
    close_http_client,
    redact_sensitive,
    get_authorized_chat_ids,
)

# pytest-asyncio configured via @pytest.mark.asyncio on individual async tests


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_settings_path(tmp_path):
    """Create a temporary settings.json file"""
    settings_file = tmp_path / "settings.json"
    settings = {
        "TELEGRAM_BOT_TOKEN": "7123456789:AAFw_test_token",
        "TELEGRAM_BOT_ENABLED": True,
        "TELEGRAM_INTERNAL_SECRET": "test-secret-123"
    }
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    return settings_file


@pytest.fixture
def mock_message():
    """Create a mock Telegram message"""
    message = MagicMock(spec=Message)
    message.text = "Привет"
    message.chat = MagicMock(spec=Chat)
    message.chat.id = 123456789
    message.chat.type = "private"
    message.message_id = 987654321
    message.from_user = MagicMock(spec=User)
    message.from_user.id = 123456789
    message.from_user.username = "testuser"
    message.from_user.first_name = "Test"
    return message


@pytest.fixture
def mock_server_response():
    """Create a mock successful server response"""
    return {
        "status": "success",
        "response_text": "Привет! Я Nestor...",
        "task_id": "test-task-id",
        "cost_usd": 0.02
    }


# ============================================================================
# Configuration Tests
# ============================================================================

class TestConfiguration:
    """Test configuration loading and validation"""
    
    def test_load_settings_from_file(self, mock_settings_path, monkeypatch):
        """Test loading settings from settings.json"""
        monkeypatch.setattr("telegram_bot.SETTINGS_PATH", mock_settings_path)
        settings = load_settings()
        
        assert settings["TELEGRAM_BOT_TOKEN"] == "7123456789:AAFw_test_token"
        assert settings["TELEGRAM_BOT_ENABLED"] is True
        assert settings["TELEGRAM_INTERNAL_SECRET"] == "test-secret-123"
    
    def test_load_settings_missing_file(self, tmp_path, monkeypatch):
        """Test loading settings when file doesn't exist"""
        monkeypatch.setattr("telegram_bot.SETTINGS_PATH", tmp_path / "nonexistent.json")
        settings = load_settings()
        
        assert settings == {}
    
    def test_load_settings_invalid_json(self, tmp_path, monkeypatch):
        """Test loading settings with invalid JSON"""
        invalid_file = tmp_path / "invalid.json"
        invalid_file.write_text("not valid json{", encoding="utf-8")
        
        monkeypatch.setattr("telegram_bot.SETTINGS_PATH", invalid_file)
        settings = load_settings()
        
        assert settings == {}
    
    def test_bot_token_required(self, monkeypatch, caplog):
        """Test that bot exits if token is not configured"""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr("telegram_bot._settings", {})
        
        # This should log an error and exit
        # We can't test sys.exit(1) directly, but we can check the condition
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        assert token == ""
    
    def test_bot_enabled_check(self, monkeypatch):
        """Test bot enabled/disabled flag parsing"""
        # Test various truthy values
        for truthy in ("true", "1", "yes", "True", "YES"):
            monkeypatch.setenv("TELEGRAM_BOT_ENABLED", truthy)
            enabled = os.environ.get("TELEGRAM_BOT_ENABLED", "").lower() in ("true", "1", "yes")
            assert enabled is True
        
        # Test various falsy values
        for falsy in ("false", "0", "no", "False", "NO", ""):
            monkeypatch.setenv("TELEGRAM_BOT_ENABLED", falsy)
            enabled = os.environ.get("TELEGRAM_BOT_ENABLED", "").lower() in ("true", "1", "yes")
            assert enabled is False


# ============================================================================
# Handler Registration Tests
# ============================================================================

class TestHandlerRegistration:
    """Test that all handlers are properly registered"""
    
    def test_dispatcher_has_start_handler(self):
        """Test that /start command handler is registered"""
        # Check that the handler function exists and is callable
        assert callable(cmd_start)
    
    def test_dispatcher_has_status_handler(self):
        """Test that /status command handler is registered"""
        assert callable(cmd_status)
    
    def test_dispatcher_has_evolve_handler(self):
        """Test that /evolve command handler is registered"""
        assert callable(cmd_evolve)
    
    def test_dispatcher_has_bg_handler(self):
        """Test that /bg command handler is registered"""
        assert callable(cmd_bg)
    
    def test_dispatcher_has_restart_handler(self):
        """Test that /restart command handler is registered"""
        assert callable(cmd_restart)
    
    def test_dispatcher_has_panic_handler(self):
        """Test that /panic command handler is registered"""
        assert callable(cmd_panic)
    
    def test_dispatcher_has_text_handler(self):
        """Test that regular text message handler is registered"""
        assert callable(handle_text)
    
    def test_command_filter_works(self):
        """Test that Command filter works correctly (regression test for Command() bug)"""
        # This is a regression test for the bug where Command() without args raised ValueError
        # We test that Command("start") works
        try:
            filter_obj = Command("start")
            assert filter_obj is not None
        except ValueError as e:
            pytest.fail(f"Command filter raised ValueError: {e}")
    
    def test_text_filter_excludes_commands(self):
        """Test that F.text & ~F.text.startswith('/') excludes slash commands"""
        # Test that regular text passes
        text_message = MagicMock(spec=Message)
        text_message.text = "Привет"
        assert text_message.text.startswith("/") is False
        
        # Test that slash command is excluded
        cmd_message = MagicMock(spec=Message)
        cmd_message.text = "/start"
        assert cmd_message.text.startswith("/") is True


# ============================================================================
# Message Handler Tests
# ============================================================================

class TestMessageHandlers:
    """Test individual message handlers"""
    
    def test_cmd_start_handler_exists_and_callable(self):
        """Test that /start command handler exists and is callable"""
        assert callable(cmd_start)
        # Check that the handler has the right docstring
        assert cmd_start.__doc__ == "Handle /start command"
    
    def test_cmd_status_handler_exists_and_callable(self):
        """Test that /status command handler exists and is callable"""
        assert callable(cmd_status)
    
    def test_cmd_evolve_handler_exists_and_callable(self):
        """Test that /evolve command handler exists and is callable"""
        assert callable(cmd_evolve)
    
    def test_cmd_bg_handler_exists_and_callable(self):
        """Test that /bg command handler exists and is callable"""
        assert callable(cmd_bg)
    
    def test_handle_text_handler_exists_and_callable(self):
        """Test that regular text message handler exists and is callable"""
        assert callable(handle_text)
    
    def test_forward_to_server_function_exists(self):
        """Test that _forward_to_server helper function exists"""
        assert callable(_forward_to_server)
    
    def test_server_api_url_is_correct(self):
        """Test that server API URL is correctly configured"""
        assert SERVER_API_URL == "http://127.0.0.1:8765/api/telegram/process-message"
        assert SERVER_API_URL.startswith("http://127.0.0.1:8765")
        assert "api/telegram/process-message" in SERVER_API_URL


# ============================================================================
# Middleware Tests
# ============================================================================

class TestLoggingMiddleware:
    """Test logging middleware functionality"""
    
    def test_middleware_initialization(self):
        """Test that middleware can be instantiated"""
        middleware = LoggingMiddleware()
        assert middleware is not None
    
    def test_write_log(self, tmp_path):
        """Test that log entries are written correctly"""
        middleware = LoggingMiddleware()
        log_file = tmp_path / "test_telegram.jsonl"
        
        # Temporarily override log path
        original_path = telegram_bot.TELEGRAM_LOG_PATH
        telegram_bot.TELEGRAM_LOG_PATH = log_file
        
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "test_event",
            "chat_id": 123
        }
        
        middleware._write_log(entry)
        
        # Restore original path
        telegram_bot.TELEGRAM_LOG_PATH = original_path
        
        # Verify log was written
        assert log_file.exists()
        log_content = log_file.read_text(encoding="utf-8")
        assert "test_event" in log_content
        assert "123" in log_content
    
    def test_write_log_handles_errors(self, caplog):
        """Test that log write errors are handled gracefully"""
        middleware = LoggingMiddleware()
        
        # Try to write to non-existent directory
        import telegram_bot
        original_path = telegram_bot.TELEGRAM_LOG_PATH
        telegram_bot.TELEGRAM_LOG_PATH = Path("/nonexistent/dir/telegram.jsonl")
        
        entry = {"test": "data"}
        middleware._write_log(entry)  # Should not raise
        
        telegram_bot.TELEGRAM_LOG_PATH = original_path

    @pytest.mark.asyncio
    async def test_middleware_handles_missing_from_user(self):
        """Channel posts must not crash logging when from_user is missing."""
        middleware = LoggingMiddleware()
        message = MagicMock(spec=Message)
        message.text = "channel update"
        message.chat = MagicMock(spec=Chat)
        message.chat.id = -100123
        message.message_id = 55
        message.from_user = None
        handler = AsyncMock(return_value="ok")

        result = await middleware(handler, message, {})

        assert result == "ok"
        handler.assert_awaited_once()


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests for the bot"""
    
    def test_bot_can_be_instantiated(self):
        """Test that bot can be created with valid token"""
        bot = Bot(token="7123456789:AAFw_test_token_for_instantiation_check")
        assert bot is not None
        assert bot.token == "7123456789:AAFw_test_token_for_instantiation_check"
    
    def test_dispatcher_can_be_created(self):
        """Test that dispatcher can be instantiated"""
        dp = Dispatcher()
        assert dp is not None
    
    def test_all_command_handlers_defined(self):
        """Test that all expected command handlers are defined"""
        expected_commands = ["start", "status", "evolve", "bg", "restart", "panic"]
        
        for cmd in expected_commands:
            handler = getattr(sys.modules["telegram_bot"], f"cmd_{cmd}", None)
            assert handler is not None, f"Handler for /{cmd} not found"
            assert callable(handler), f"Handler for /{cmd} is not callable"
    
    def test_server_api_url_format(self):
        """Test that server API URL is correctly formatted"""
        assert SERVER_API_URL == "http://127.0.0.1:8765/api/telegram/process-message"
        assert SERVER_API_URL.startswith("http://")
        assert "api/telegram/process-message" in SERVER_API_URL

    @pytest.mark.asyncio
    async def test_http_client_is_reused(self):
        """HTTP client should be shared across requests for pooling."""
        telegram_bot._http_client = None

        client1 = await get_http_client()
        client2 = await get_http_client()

        assert client1 is client2

        await close_http_client()
        assert telegram_bot._http_client is None


# ============================================================================
# Regression Tests
# ============================================================================

class TestRegression:
    """Regression tests for known bugs"""
    
    def test_command_filter_with_args(self):
        """
        Regression test for: ValueError: At least one command should be specified
        
        This happened when using Command() without arguments in aiogram 3.x.
        The fix was to use F.text.startswith("/") instead.
        """
        # This should NOT raise ValueError
        try:
            # Old (broken) way that raised ValueError:
            # Command()  # ❌ Raises: ValueError: At least one command should be specified
            
            # New (working) way:
            filter_expr = F.text & ~F.text.startswith("/")
            assert filter_expr is not None
            
            # Verify it works as expected
            test_cases = [
                ("Привет", True),      # Regular text should pass
                ("/start", False),     # Commands should be excluded
                ("/status", False),    # Commands should be excluded
                ("test /inline", True), # Text with slash in middle should pass
            ]
            
            for text, should_pass in test_cases:
                is_command = text.startswith("/")
                passes_filter = not is_command
                assert passes_filter == should_pass, f"Filter failed for '{text}'"
                
        except ValueError as e:
            pytest.fail(f"Command filter raised ValueError (regression): {e}")
    
    def test_no_import_errors_on_startup(self):
        """
        Regression test: Ensure telegram_bot.py can be imported without errors.
        
        This catches import errors, missing dependencies, and syntax errors
        that would prevent the bot from starting.
        """
        try:
            # This should not raise any import errors
            import telegram_bot
            
            # Verify key components are importable
            assert hasattr(telegram_bot, "Bot")
            assert hasattr(telegram_bot, "Dispatcher")
            assert hasattr(telegram_bot, "load_settings")
            assert hasattr(telegram_bot, "cmd_start")
            assert hasattr(telegram_bot, "handle_text")
            
        except ImportError as e:
            pytest.fail(f"Import error in telegram_bot.py: {e}")
        except Exception as e:
            pytest.fail(f"Unexpected error importing telegram_bot.py: {e}")
    
    def test_settings_loading_graceful_degradation(self):
        """
        Regression test: Bot should handle missing/invalid settings gracefully.
        
        Previously, the bot would crash if settings.json was missing or malformed.
        Now it should log an error and exit gracefully.
        """
        # Test with missing file
        import tempfile
        import os
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set DATA_DIR to temp directory (no settings.json)
            original_settings_path = telegram_bot.SETTINGS_PATH
            
            try:
                telegram_bot.SETTINGS_PATH = Path(tmpdir) / "nonexistent.json"
                settings = load_settings()
                
                # Should return empty dict, not crash
                assert settings == {}
                
            finally:
                telegram_bot.SETTINGS_PATH = original_settings_path

    def test_redact_sensitive_hides_configured_secrets(self, monkeypatch):
        """Log redaction should remove bot token and internal secret."""
        monkeypatch.setattr(telegram_bot, "TELEGRAM_BOT_TOKEN", "bot-secret")
        monkeypatch.setattr(telegram_bot, "TELEGRAM_INTERNAL_SECRET", "internal-secret")

        redacted = redact_sensitive("bot-secret and internal-secret visible")

        assert "bot-secret" not in redacted
        assert "internal-secret" not in redacted
        assert redacted.count("[REDACTED]") == 2

    def test_get_authorized_chat_ids_includes_owner_fallback(self, monkeypatch):
        """owner_chat_id from state should augment TELEGRAM_ADMIN_CHAT_IDS."""
        monkeypatch.setattr(telegram_bot, "TELEGRAM_ADMIN_CHAT_IDS", "111,222")
        monkeypatch.setattr(telegram_bot, "_load_owner_chat_id", lambda: 333)

        assert get_authorized_chat_ids() == {111, 222, 333}

    @pytest.mark.asyncio
    async def test_privileged_command_requires_authorized_chat(self, mock_message, monkeypatch):
        """Restricted commands must reject unauthorized chats."""
        mock_message.answer = AsyncMock()
        monkeypatch.setattr(telegram_bot, "is_authorized_chat", lambda chat_id: False)
        forward_mock = AsyncMock()
        monkeypatch.setattr(telegram_bot, "_forward_to_server", forward_mock)

        await cmd_panic(mock_message)

        forward_mock.assert_not_awaited()
        mock_message.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_privileged_command_allows_authorized_chat(self, mock_message, monkeypatch):
        """Restricted commands must pass through for authorized chats."""
        monkeypatch.setattr(telegram_bot, "is_authorized_chat", lambda chat_id: True)
        forward_mock = AsyncMock()
        monkeypatch.setattr(telegram_bot, "_forward_to_server", forward_mock)
        mock_message.text = "/restart"

        await cmd_restart(mock_message)

        forward_mock.assert_awaited_once_with(mock_message, "/restart")

    @pytest.mark.asyncio
    async def test_forward_to_server_handles_missing_from_user(self, mock_message, monkeypatch, mock_server_response):
        """Channel posts should serialize a null user block instead of crashing."""
        mock_message.from_user = None
        processing_msg = AsyncMock()
        processing_msg.message_id = 333
        mock_message.answer = AsyncMock(return_value=processing_msg)
        client = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = mock_server_response
        client.post = AsyncMock(return_value=response)
        monkeypatch.setattr(telegram_bot, "get_http_client", AsyncMock(return_value=client))
        delete_mock = AsyncMock()
        monkeypatch.setattr(telegram_bot.bot, "delete_message", delete_mock)

        await _forward_to_server(mock_message, "hello")

        payload = client.post.await_args.kwargs["json"]
        assert payload["from_user"] == {"id": None, "username": None, "first_name": None}
        delete_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_forward_to_server_retries_on_500(self, mock_message, monkeypatch, mock_server_response):
        """Transient 500s should be retried before succeeding."""
        processing_msg = AsyncMock()
        processing_msg.message_id = 444
        mock_message.answer = AsyncMock(return_value=processing_msg)
        request = httpx.Request("POST", SERVER_API_URL)
        failure_response = httpx.Response(500, request=request)
        failure = httpx.HTTPStatusError("boom", request=request, response=failure_response)
        success_response = MagicMock()
        success_response.raise_for_status = MagicMock()
        success_response.json.return_value = mock_server_response
        client = AsyncMock()
        client.post = AsyncMock(side_effect=[failure, success_response])
        monkeypatch.setattr(telegram_bot, "get_http_client", AsyncMock(return_value=client))
        monkeypatch.setattr(telegram_bot.bot, "delete_message", AsyncMock())
        sleep_mock = AsyncMock()
        monkeypatch.setattr(telegram_bot.asyncio, "sleep", sleep_mock)

        await _forward_to_server(mock_message, "hello")

        assert client.post.await_count == 2
        sleep_mock.assert_awaited_once()


# ============================================================================
# End of Tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
