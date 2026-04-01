#!/usr/bin/env python3
"""
Telegram Bot — Standalone aiogram 3.x process.

Communicates with server.py via HTTP API for message processing.
Runs as a separate subprocess managed by launcher.py.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("OUROBOROS_DATA_DIR", Path.home() / "Ouroboros" / "data"))
SETTINGS_PATH = DATA_DIR / "settings.json"
TELEGRAM_LOG_PATH = DATA_DIR / "logs" / "telegram.jsonl"
STATE_PATH = DATA_DIR / "state" / "state.json"

# Ensure log directory exists
TELEGRAM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Logging
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_handlers.append(logging.FileHandler(TELEGRAM_LOG_PATH.with_name("telegram_bot.log")))
except OSError:
    logging.getLogger("telegram_bot.bootstrap").warning(
        "Telegram bot file logging disabled: log path is not writable"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("telegram_bot")

# Load settings from settings.json (same as server.py)
def load_settings():
    """Load settings from settings.json file"""
    if not SETTINGS_PATH.exists():
        log.error("Settings file not found: %s", SETTINGS_PATH)
        return {}
    
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load settings: %s", e)
        return {}

_settings = load_settings()

# Config from settings.json (with env override for launcher-managed runs)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", _settings.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_BOT_ENABLED = os.environ.get("TELEGRAM_BOT_ENABLED", str(_settings.get("TELEGRAM_BOT_ENABLED", False))).lower() in ("true", "1", "yes", "yes")
TELEGRAM_INTERNAL_SECRET = os.environ.get("TELEGRAM_INTERNAL_SECRET", _settings.get("TELEGRAM_INTERNAL_SECRET", ""))
TELEGRAM_ADMIN_CHAT_IDS = os.environ.get("TELEGRAM_ADMIN_CHAT_IDS", _settings.get("TELEGRAM_ADMIN_CHAT_IDS", ""))
_IMPORT_PLACEHOLDER_TOKEN = "7123456789:AAFw_import_placeholder_token"

SERVER_API_URL = f"http://127.0.0.1:8765/api/telegram/process-message"
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


def _parse_chat_ids(raw_value) -> set[int]:
    """Normalize admin chat-id config from env/settings into a set of ints."""
    if raw_value in (None, ""):
        return set()
    if isinstance(raw_value, int):
        return {raw_value}

    values = raw_value if isinstance(raw_value, list) else [part.strip() for part in str(raw_value).split(",")]
    parsed: set[int] = set()
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed.add(int(value))
        except (TypeError, ValueError):
            log.warning("Ignoring invalid authorized Telegram chat_id value")
    return parsed


def _load_owner_chat_id() -> Optional[int]:
    """Read owner_chat_id from supervisor state when available."""
    try:
        if not STATE_PATH.exists():
            return None
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        owner_chat_id = state.get("owner_chat_id")
        return int(owner_chat_id) if owner_chat_id is not None else None
    except Exception:
        log.warning("Failed to read owner_chat_id from Telegram state")
        return None


def get_authorized_chat_ids() -> set[int]:
    """Return chat IDs allowed to use privileged Telegram commands."""
    chat_ids = _parse_chat_ids(TELEGRAM_ADMIN_CHAT_IDS)
    owner_chat_id = _load_owner_chat_id()
    if owner_chat_id is not None:
        chat_ids.add(owner_chat_id)
    return chat_ids


def is_authorized_chat(chat_id: int) -> bool:
    """Check whether a chat may use privileged commands."""
    authorized_chat_ids = get_authorized_chat_ids()
    return bool(authorized_chat_ids) and chat_id in authorized_chat_ids


def redact_sensitive(value: object) -> str:
    """Best-effort secret redaction for log-safe strings."""
    text = str(value)
    for secret in (TELEGRAM_BOT_TOKEN, TELEGRAM_INTERNAL_SECRET):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _message_user_dict(message: Message) -> dict:
    """Build a payload user block without assuming from_user exists."""
    if message.from_user is None:
        return {"id": None, "username": None, "first_name": None}
    return {
        "id": message.from_user.id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
    }


def _message_actor_label(message: Message) -> str:
    """Compact log label for the message actor."""
    if message.from_user is None:
        return "channel_or_anonymous"
    return message.from_user.username or str(message.from_user.id)


async def _ensure_authorized(message: Message, command_name: str) -> bool:
    """Reject privileged commands from unauthorized chats."""
    if is_authorized_chat(message.chat.id):
        return True
    log.warning("Rejected unauthorized Telegram command %s for chat %s", command_name, message.chat.id)
    await message.answer("⛔ Эта команда недоступна для этого чата.")
    return False


def _validate_runtime_configuration() -> int:
    """Return process exit code for invalid runtime config, 0 when start is allowed."""
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not configured. Exiting.")
        return 1
    if not TELEGRAM_BOT_ENABLED:
        log.info("Telegram bot is disabled. Exiting.")
        return 0
    return -1


def _ensure_internal_secret(persist: bool = True) -> bool:
    """Ensure TELEGRAM_INTERNAL_SECRET is available before network operations."""
    global TELEGRAM_INTERNAL_SECRET
    if TELEGRAM_INTERNAL_SECRET:
        return True

    import secrets

    TELEGRAM_INTERNAL_SECRET = secrets.token_urlsafe(32)
    _settings["TELEGRAM_INTERNAL_SECRET"] = TELEGRAM_INTERNAL_SECRET
    if not persist:
        return True

    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(_settings, f, indent=2, ensure_ascii=False)
        log.info("Auto-generated TELEGRAM_INTERNAL_SECRET and saved to settings.json")
        return True
    except Exception:
        log.error("Failed to save auto-generated internal secret. Exiting.")
        return False

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_ENABLED:
    log.info("Telegram bot starting (polling mode)...")

# ---------------------------------------------------------------------------
# Bot initialization
# ---------------------------------------------------------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN or _IMPORT_PLACEHOLDER_TOKEN)
dp = Dispatcher()

# HTTP client for server communication
_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client():
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"X-Telegram-Secret": TELEGRAM_INTERNAL_SECRET},
        )
    return _http_client


async def close_http_client():
    """Close the shared HTTP client on shutdown."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None

# ---------------------------------------------------------------------------
# Logging middleware
# ---------------------------------------------------------------------------
class LoggingMiddleware:
    """Log all incoming messages to telegram.jsonl"""
    
    async def __call__(self, handler, event, data):
        start_time = time.time()
        
        # Log incoming message
        if isinstance(event, Message) and event.text:
            log_entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "message_received",
                "chat_id": event.chat.id,
                "message_id": event.message_id,
                "text": redact_sensitive(event.text[:500]),
                "from_user": _message_actor_label(event),
            }
            self._write_log(log_entry)
        
        result = await handler(event, data)
        duration = time.time() - start_time
        
        log.debug("Handler completed in %.2fms", duration * 1000)
        return result
    
    def _write_log(self, entry: dict):
        try:
            with open(TELEGRAM_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error("Failed to write telegram log: %s", e)

# Install middleware
dp.message.outer_middleware(LoggingMiddleware())

# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Handle /start command"""
    response_text = (
        "Привет! Я Nestor, самосозидающийся цифровой агент. 🤖\n\n"
        "Я могу:\n"
        "• Вести диалог на любые темы\n"
        "• Редактировать свой собственный код\n"
        "• Работать с файлами и выполнять команды\n"
        "• Иметь фоновые размышления и эволюционировать\n\n"
        "Доступные команды:\n"
        "/start — это сообщение\n"
        "/status — состояние системы\n"
        "/evolve — включить/выключить эволюцию\n"
        "/bg — фоновые размышления\n"
        "/restart — перезагрузка\n"
        "/panic — экстренная остановка\n\n"
        "Просто пиши мне сообщения, и я отвечу!"
    )
    await message.answer(response_text)

@dp.message(Command("status"))
async def cmd_status(message: Message):
    """Handle /status command — forward to server"""
    await _forward_to_server(message, "/status")

@dp.message(Command("evolve"))
async def cmd_evolve(message: Message):
    """Handle /evolve command"""
    if not await _ensure_authorized(message, "/evolve"):
        return
    # Parse on/off from argument
    arg = message.text.split(None, 1)[1].strip().lower() if " " in message.text else "on"
    await _forward_to_server(message, f"/evolve {arg}")

@dp.message(Command("bg"))
async def cmd_bg(message: Message):
    """Handle /bg command"""
    if not await _ensure_authorized(message, "/bg"):
        return
    arg = message.text.split(None, 1)[1].strip().lower() if " " in message.text else "start"
    await _forward_to_server(message, f"/bg {arg}")

@dp.message(Command("restart"))
async def cmd_restart(message: Message):
    """Handle /restart command"""
    if not await _ensure_authorized(message, "/restart"):
        return
    await _forward_to_server(message, "/restart")

@dp.message(Command("panic"))
async def cmd_panic(message: Message):
    """Handle /panic command"""
    if not await _ensure_authorized(message, "/panic"):
        return
    await _forward_to_server(message, "/panic")

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    """Handle regular text messages (not commands)"""
    if not message.text:
        return
    
    # Forward to server for LLM processing
    await _forward_to_server(message, message.text)

async def _forward_to_server(message: Message, text: str):
    """Forward message to server.py for processing"""
    chat_id = message.chat.id
    message_id = message.message_id
    
    # Send processing indicator
    processing_msg = await message.answer("⏳ Обрабатываю...")
    
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "from_user": _message_user_dict(message),
        "task_type": "telegram_message"
    }
    
    try:
        client = await get_http_client()
        # Retry logic with exponential backoff
        for attempt in range(3):
            try:
                response = await client.post(SERVER_API_URL, json=payload)
                response.raise_for_status()
                result = response.json()

                if result.get("status") == "success":
                    # Delete processing indicator
                    await bot.delete_message(chat_id=chat_id, message_id=processing_msg.message_id)
                    log.info("Message queued for chat %s", chat_id)
                    return

                # Server returned error
                error_msg = result.get("message", "Ошибка обработки сообщения")
                await processing_msg.edit_text(f"⚠️ {error_msg}")
                return

            except httpx.TimeoutException:
                if attempt == 2:
                    await processing_msg.edit_text("⚠️ Тайм-аут сервера. Попробуйте позже.")
                    log.error("Server timeout after retries")
                    return
                await asyncio.sleep(2 ** attempt)

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if status_code in RETRYABLE_STATUS_CODES:
                    if attempt == 2:
                        await processing_msg.edit_text("⚠️ Сервер временно недоступен. Попробуйте позже.")
                        log.error("Server %s after retries for chat %s", status_code, chat_id)
                        return
                    await asyncio.sleep(2 ** attempt)
                    continue

                await processing_msg.edit_text(f"⚠️ Ошибка сервера: {status_code}")
                log.warning("Non-retryable Telegram bot HTTP error %s for chat %s", status_code, chat_id)
                return

        # Fallback if all retries failed
        await processing_msg.edit_text("⚠️ Не удалось связаться с сервером.")

    except Exception as e:
        log.error("Failed to forward message to server: %s", redact_sensitive(f"{type(e).__name__}: {e}"))
        await processing_msg.edit_text("⚠️ Внутренняя ошибка. Попробуйте позже.")

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def main():
    """Start the bot with polling"""
    exit_code = _validate_runtime_configuration()
    if exit_code >= 0:
        raise SystemExit(exit_code)
    if not _ensure_internal_secret(persist=True):
        raise SystemExit(1)
    log.info("Starting Telegram bot polling...")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        log.info("Telegram bot shutting down")
    finally:
        await close_http_client()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
