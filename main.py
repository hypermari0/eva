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

import agent
import memory

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

REMINDER_CHECK_INTERVAL = 60  # seconds


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey! I'm Eva, your personal AI assistant. Send me a message and I'll do my best to help."
    )


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 60 seconds
    app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_INTERVAL, first=10)

    logger.info("Eva is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
