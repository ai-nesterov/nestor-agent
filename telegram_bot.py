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
TELEGRAM_LOG_PATH = DATA_DIR / "logs" / "telegram.jsonl"

# Ensure log directory exists
TELEGRAM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(TELEGRAM_LOG_PATH.with_name("telegram_bot.log"))
    ]
)
log = logging.getLogger("telegram_bot")

# Config from environment
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_ENABLED = os.environ.get("TELEGRAM_BOT_ENABLED", "false").lower() in ("true", "1", "yes", "yes")
TELEGRAM_INTERNAL_SECRET = os.environ.get("TELEGRAM_INTERNAL_SECRET", "")

SERVER_API_URL = f"http://127.0.0.1:8765/api/telegram/process-message"

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
if not TELEGRAM_BOT_TOKEN:
    log.error("TELEGRAM_BOT_TOKEN not configured. Exiting.")
    sys.exit(1)

if not TELEGRAM_BOT_ENABLED:
    log.info("Telegram bot is disabled. Exiting.")
    sys.exit(0)

if not TELEGRAM_INTERNAL_SECRET:
    log.error("TELEGRAM_INTERNAL_SECRET not configured. Exiting.")
    sys.exit(1)

log.info("Telegram bot starting on port %s", TELEGRAM_BOT_PORT)

# ---------------------------------------------------------------------------
# Bot initialization
# ---------------------------------------------------------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# HTTP client for server communication
async def get_http_client():
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        headers={"X-Telegram-Secret": TELEGRAM_INTERNAL_SECRET}
    )

# ---------------------------------------------------------------------------
# Logging middleware
# ---------------------------------------------------------------------------
class LoggingMiddleware:
    """Log all incoming messages to telegram.jsonl"""
    
    async def __call__(self, handler, event):
        start_time = time.time()
        
        # Log incoming message
        if isinstance(event, Message) and event.text:
            log_entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "message_received",
                "chat_id": event.chat.id,
                "message_id": event.message_id,
                "text": event.text[:500],  # Truncate for log
                "from_user": event.from_user.username or str(event.from_user.id)
            }
            self._write_log(log_entry)
        
        result = await handler(event)
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
    # Parse on/off from argument
    arg = message.text.split(None, 1)[1].strip().lower() if " " in message.text else "on"
    await _forward_to_server(message, f"/evolve {arg}")

@dp.message(Command("bg"))
async def cmd_bg(message: Message):
    """Handle /bg command"""
    arg = message.text.split(None, 1)[1].strip().lower() if " " in message.text else "start"
    await _forward_to_server(message, f"/bg {arg}")

@dp.message(Command("restart"))
async def cmd_restart(message: Message):
    """Handle /restart command"""
    await _forward_to_server(message, "/restart")

@dp.message(Command("panic"))
async def cmd_panic(message: Message):
    """Handle /panic command"""
    await _forward_to_server(message, "/panic")

@dp.message(F.text & ~Command())
async def handle_text(message: Message):
    """Handle regular text messages"""
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
        "from_user": {
            "id": message.from_user.id,
            "username": message.from_user.username,
            "first_name": message.from_user.first_name
        },
        "task_type": "telegram_message"
    }
    
    try:
        async with get_http_client() as client:
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
                        log.error("Server timeout after 3 attempts")
                        return
                    await asyncio.sleep(2 ** attempt)
                    
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 503:
                        if attempt == 2:
                            await processing_msg.edit_text("⚠️ Сервер временно недоступен. Попробуйте позже.")
                            log.error("Server unavailable after 3 attempts")
                            return
                        await asyncio.sleep(2 ** attempt)
                    else:
                        await processing_msg.edit_text(f"⚠️ Ошибка сервера: {e.response.status_code}")
                        return
            
            # Fallback if all retries failed
            await processing_msg.edit_text("⚠️ Не удалось связаться с сервером.")
            
    except Exception as e:
        log.error("Failed to forward message to server: %s", e, exc_info=True)
        await processing_msg.edit_text("⚠️ Внутренняя ошибка. Попробуйте позже.")

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def main():
    """Start the bot with polling"""
    log.info("Starting Telegram bot polling...")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        log.info("Telegram bot shutting down")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())