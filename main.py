"""Eva — Telegram bot entry point."""

import logging
import os
import time
from collections import defaultdict

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
import tools

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

REMINDER_CHECK_INTERVAL = 60  # seconds

# --- Access control ---
_allowed_ids_raw = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _allowed_ids_raw.split(",") if uid.strip()
}

# --- Rate limiting ---
RATE_LIMIT_MESSAGES = int(os.environ.get("RATE_LIMIT_MESSAGES", "10"))  # per window
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # seconds
_user_message_times: dict[int, list[float]] = defaultdict(list)


def _is_allowed(user_id: int) -> bool:
    """Check if a user is in the allowlist. If no allowlist is set, allow all."""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def _is_rate_limited(user_id: int) -> bool:
    """Check if a user has exceeded the message rate limit."""
    now = time.monotonic()
    times = _user_message_times[user_id]
    # Prune old entries
    _user_message_times[user_id] = [t for t in times if now - t < RATE_LIMIT_WINDOW]
    if len(_user_message_times[user_id]) >= RATE_LIMIT_MESSAGES:
        return True
    _user_message_times[user_id].append(now)
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    reply = await agent.chat(update.effective_user.id, "/start")
    await update.message.reply_text(reply)


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /connect <app> — initiate OAuth for an external service."""
    if not _is_allowed(update.effective_user.id):
        return
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


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /approve <skill_name> — activate a pending skill."""
    if not _is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /approve <skill_name>")
        return

    name = context.args[0].lower().strip()
    skill = memory.get_dynamic_skill(name)

    if not skill:
        await update.message.reply_text(f"No skill named '{name}' found.")
        return

    if skill["status"] == "approved":
        await update.message.reply_text(f"'{name}' is already approved and active.")
        return

    if skill["status"] != "pending":
        await update.message.reply_text(f"'{name}' is in status '{skill['status']}' and can't be approved.")
        return

    # Only the user who created the skill (or an admin) can approve it
    if skill["created_by"] != update.effective_user.id:
        await update.message.reply_text("You can only approve skills you created.")
        return

    # Approve in DB
    memory.approve_dynamic_skill(name)

    # Register in-memory so it's available immediately
    tools.register_dynamic_skill(
        name=skill["name"],
        description=skill["description"],
        parameters=skill["parameters"],
        code=skill["code"],
    )

    await update.message.reply_text(f"Skill '{name}' approved and active. I can use it now.")


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reject <skill_name> — discard a pending skill."""
    if not _is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /reject <skill_name>")
        return

    name = context.args[0].lower().strip()
    skill = memory.get_dynamic_skill(name)

    if not skill:
        await update.message.reply_text(f"No skill named '{name}' found.")
        return

    memory.delete_dynamic_skill(name)
    tools.unregister_dynamic_skill(name)
    await update.message.reply_text(f"Skill '{name}' rejected and discarded.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice/audio messages — transcribe via Groq Whisper, then process as text."""
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return
    if _is_rate_limited(user_id):
        await update.message.reply_text("You're sending messages too fast. Please wait a moment.")
        return
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
    if not _is_allowed(user_id):
        return
    if _is_rate_limited(user_id):
        await update.message.reply_text("You're sending messages too fast. Please wait a moment.")
        return

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
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 60 seconds
    app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_INTERVAL, first=10)

    logger.info("Eva is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
