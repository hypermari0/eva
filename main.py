"""Eva — Telegram bot entry point."""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import httpx

import agent
import composio_bridge
import memory

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

REMINDER_CHECK_INTERVAL = 60  # seconds


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = await agent.chat(update.effective_user.id, "/start")
    await update.message.reply_text(reply)


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /connect <app> — initiate OAuth for an external service."""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /connect <app>\n\n"
            "Available apps: googlecalendar, gmail, slack, github\n\n"
            "Example: /connect googlecalendar"
        )
        return

    app_name = args[0].lower().strip()
    entity_id = str(update.effective_user.id)

    # Check if already connected
    if composio_bridge.check_connection(entity_id, app_name):
        await update.message.reply_text(f"You're already connected to {app_name}!")
        return

    url = composio_bridge.initiate_connection(entity_id, app_name)
    if url:
        await update.message.reply_text(
            f"Click this link to connect {app_name}:\n\n{url}\n\n"
            "Once you've authorized, come back and I'll have access."
        )
    else:
        await update.message.reply_text(
            f"Failed to start connection for {app_name}. "
            "Make sure COMPOSIO_API_KEY is set and the app name is correct."
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice/audio messages — transcribe via Groq Whisper, then process as text."""
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    user_id = update.effective_user.id
    await update.message.chat.send_action("typing")

    try:
        # Download voice file from Telegram
        file = await context.bot.get_file(voice.file_id)
        buf = bytearray()
        await file.download_as_bytearray(buf)

        # Transcribe via Groq Whisper API
        groq_key = os.environ.get("GROQ_API_KEY")
        if not groq_key:
            await update.message.reply_text("Voice messages aren't configured yet (missing GROQ_API_KEY).")
            return

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": ("voice.ogg", bytes(buf), "audio/ogg")},
                data={"model": "whisper-large-v3"},
            )
            resp.raise_for_status()
            text = resp.json()["text"]

        if not text.strip():
            await update.message.reply_text("I couldn't understand the audio. Could you try again?")
            return

        # Process transcribed text through the normal agent flow
        reply = await agent.chat(user_id, text)
    except Exception:
        logger.exception("Error processing voice message")
        reply = "Sorry, something went wrong processing your voice message."

    for i in range(0, len(reply), 4096):
        await update.message.reply_text(reply[i : i + 4096])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text

    await update.message.chat.send_action("typing")

    try:
        reply = await agent.chat(user_id, text)
    except Exception:
        logger.exception("Error processing message")
        reply = "Sorry, something went wrong. Please try again."

    # Telegram has a 4096-char limit per message
    for i in range(0, len(reply), 4096):
        await update.message.reply_text(reply[i : i + 4096])


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: send due reminders to users."""
    try:
        pending = memory.get_pending_reminders()
    except Exception:
        logger.exception("Failed to fetch reminders")
        return

    for r in pending:
        try:
            await context.bot.send_message(
                chat_id=r["user_id"],
                text=f"Reminder: {r['message']}",
            )
            memory.mark_reminder_sent(r["id"])
        except Exception:
            logger.exception(f"Failed to send reminder {r['id']}")


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("connect", connect_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 60 seconds
    app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_INTERVAL, first=10)

    logger.info("Eva is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
