# ouroboros/tools/telegram.py — Telegram bot tools
"""
Telegram integration tools for Ouroboros.

Provides:
- telegram_send_message: Send a message to a Telegram chat
- telegram_setup_webhook: Configure webhook URL with Telegram Bot API
- telegram_get_webhook_info: Get current webhook status
- telegram_get_me: Get bot information

Configuration (via settings.json):
- TELEGRAM_BOT_TOKEN: Bot token from @BotFather
- TELEGRAM_BOT_ENABLED: "true" | "false"
- TELEGRAM_WEBHOOK_URL: Public webhook URL (for ngrok/localtunnel)
"""

import os
import urllib.request
import urllib.error
import json
from typing import Optional, List, Any, Callable

from ouroboros.tools.registry import ToolEntry, ToolContext


def _get_token() -> str:
    """Get Telegram bot token from environment."""
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _is_enabled() -> bool:
    """Check if Telegram bot is enabled."""
    return os.environ.get("TELEGRAM_BOT_ENABLED", "false").lower() in ("true", "1", "yes")


def _telegram_api_request(method: str, data: Optional[dict] = None) -> Optional[dict]:
    """
    Make a request to Telegram Bot API.
    
    Returns parsed JSON response or None on error.
    """
    token = _get_token()
    if not token:
        return None
    
    url = f"https://api.telegram.org/bot{token}/{method}"
    
    try:
        if data:
            payload = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result
    except Exception as e:
        return {"error": str(e)}


def telegram_send_message(chat_id: str, text: str, parse_mode: str = "") -> str:
    """
    Send a text message to a Telegram chat.
    
    Args:
        chat_id: Telegram chat ID (numeric or username)
        text: Message text to send (max 4096 characters)
    
    Returns:
        Status message with result details
    """
    if not _is_enabled():
        return "Telegram bot is not enabled. Enable it in Settings."
    
    token = _get_token()
    if not token:
        return "Telegram bot token is not configured. Set TELEGRAM_BOT_TOKEN in Settings."
    
    # Truncate if too long (Telegram limit)
    if len(text) > 4096:
        text = text[:4093] + "..."
    
    if parse_mode not in ("", "Markdown", "MarkdownV2", "HTML"):
        return "Failed to send message: unsupported parse_mode"

    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    result = _telegram_api_request("sendMessage", payload)
    
    if result and result.get("ok"):
        return f"Message sent successfully to chat {chat_id}"
    elif result:
        error_msg = result.get("description", result.get("error", "Unknown error"))
        return f"Failed to send message: {error_msg}"
    else:
        return "Failed to connect to Telegram API"


def telegram_setup_webhook(webhook_url: str) -> str:
    """
    Configure the webhook URL for receiving Telegram updates.
    
    Args:
        webhook_url: Full HTTPS URL where Telegram should send updates
                     (e.g., https://your-ngrok-url.ngrok.io/api/telegram/webhook)
    
    Returns:
        Status message with result details
    """
    if not _is_enabled():
        return "Telegram bot is not enabled. Enable it in Settings."
    
    token = _get_token()
    if not token:
        return "Telegram bot token is not configured. Set TELEGRAM_BOT_TOKEN in Settings."
    
    if not webhook_url or not webhook_url.startswith("https://"):
        return "Webhook URL must be a valid HTTPS URL"
    
    result = _telegram_api_request("setWebhook", {
        "url": webhook_url,
        "allowed_updates": ["message"]
    })
    
    if result and result.get("ok"):
        return f"Webhook configured successfully: {webhook_url}"
    elif result:
        error_msg = result.get("description", result.get("error", "Unknown error"))
        return f"Failed to configure webhook: {error_msg}"
    else:
        return "Failed to connect to Telegram API"


def telegram_get_webhook_info() -> str:
    """
    Get current webhook status and configuration.
    
    Returns:
        Formatted webhook information
    """
    if not _is_enabled():
        return "Telegram bot is not enabled."
    
    token = _get_token()
    if not token:
        return "Telegram bot token is not configured."
    
    result = _telegram_api_request("getWebhookInfo")
    
    if result and result.get("ok"):
        info = result.get("result", {})
        url = info.get("url", "Not set")
        pending = info.get("pending_update_count", 0)
        last_error = info.get("last_error_date", "No errors")
        
        return (
            f"Webhook URL: {url}\n"
            f"Pending updates: {pending}\n"
            f"Last error: {last_error}"
        )
    elif result:
        return f"Failed to get webhook info: {result.get('description', 'Unknown error')}"
    else:
        return "Failed to connect to Telegram API"


def telegram_get_me() -> str:
    """
    Get bot information (username, name, etc.).
    
    Returns:
        Formatted bot information
    """
    token = _get_token()
    if not token:
        return "Telegram bot token is not configured."
    
    result = _telegram_api_request("getMe")
    
    if result and result.get("ok"):
        bot = result.get("result", {})
        return (
            f"Bot Username: @{bot.get('username', 'N/A')}\n"
            f"Bot Name: {bot.get('first_name', 'N/A')}\n"
            f"ID: {bot.get('id', 'N/A')}"
        )
    elif result:
        return f"Failed to get bot info: {result.get('description', 'Unknown error')}"
    else:
        return "Failed to connect to Telegram API"


def get_tools() -> List[ToolEntry]:
    """Export Telegram tools for auto-discovery."""
    return [
        ToolEntry(
            name="telegram_send_message",
            schema={
                "name": "telegram_send_message",
                "description": "Send a text message to a Telegram chat. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_ENABLED=true in settings.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "Telegram chat ID (numeric or username)"
                        },
                        "text": {
                            "type": "string",
                            "description": "Message text to send (max 4096 characters)"
                        },
                        "parse_mode": {
                            "type": "string",
                            "description": "Optional Telegram parse mode: Markdown, MarkdownV2, or HTML. Leave empty for plain text."
                        }
                    },
                    "required": ["chat_id", "text"]
                }
            },
            handler=lambda ctx, chat_id, text, parse_mode="": telegram_send_message(chat_id, text, parse_mode=parse_mode),
            is_code_tool=False,
            timeout_sec=60
        ),
        ToolEntry(
            name="telegram_setup_webhook",
            schema={
                "name": "telegram_setup_webhook",
                "description": "Configure the webhook URL for receiving Telegram updates. Use this to set up ngrok/localtunnel URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "webhook_url": {
                            "type": "string",
                            "description": "Full HTTPS URL where Telegram should send updates (e.g., https://your-ngrok-url.ngrok.io/api/telegram/webhook)"
                        }
                    },
                    "required": ["webhook_url"]
                }
            },
            handler=lambda ctx, webhook_url: telegram_setup_webhook(webhook_url),
            is_code_tool=False,
            timeout_sec=60
        ),
        ToolEntry(
            name="telegram_get_webhook_info",
            schema={
                "name": "telegram_get_webhook_info",
                "description": "Get current webhook status and configuration from Telegram Bot API.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=lambda ctx: telegram_get_webhook_info(),
            is_code_tool=False,
            timeout_sec=60
        ),
        ToolEntry(
            name="telegram_get_me",
            schema={
                "name": "telegram_get_me",
                "description": "Get bot information (username, name, ID) from Telegram Bot API.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=lambda ctx: telegram_get_me(),
            is_code_tool=False,
            timeout_sec=60
        ),
    ]
