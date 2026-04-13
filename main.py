"""Eva — Telegram bot entry point."""

import base64
import logging
import os
import re
import time
from collections import defaultdict
from datetime import time as dt_time

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.constants import ParseMode
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


def _preprocess_markdown(text: str) -> str:
    """Pre-process LLM markdown before passing to telegramify-markdown.

    Handles structures that the library doesn't convert well:
    - ### headings → bold text (Telegram has no heading support)
    - --- horizontal rules → removed
    - Markdown tables → simplified plain lines
    """
    # Remove horizontal rules
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)

    # Convert headings to bold (strip existing ** wrapper if present)
    def _heading_to_bold(m: re.Match) -> str:
        content = m.group(1).strip()
        if content.startswith("**") and content.endswith("**"):
            content = content[2:-2]
        return f"**{content}**"

    text = re.sub(r"^#{1,6}\s+(.+)$", _heading_to_bold, text, flags=re.MULTILINE)

    # Convert markdown tables: remove separator rows, strip leading/trailing pipes
    text = re.sub(r"^\|[-\s|:]+\|$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\|\s*(.+?)\s*\|$", r"\1", text, flags=re.MULTILINE)

    # Clean up excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _to_markdownv2(text: str) -> str:
    """Convert LLM markdown to Telegram MarkdownV2 using telegramify-markdown."""
    from telegramify_markdown import markdownify

    # Disable emoji prefixes for headings (we handle headings ourselves)
    try:
        from telegramify_markdown.customize import get_runtime_config
        config = get_runtime_config()
        config.markdown_symbol.head_level_1 = ""
        config.markdown_symbol.head_level_2 = ""
        config.markdown_symbol.head_level_3 = ""
        config.markdown_symbol.head_level_4 = ""
    except ImportError:
        pass  # older/different version — headings may get emoji prefixes

    preprocessed = _preprocess_markdown(text)
    return markdownify(preprocessed)


def _strip_markdown(text: str) -> str:
    """Remove markdown markers for a clean plain-text fallback."""
    text = re.sub(r"```(?:\w*\n)?(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\|[-\s|:]+\|$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\|\s*(.+?)\s*\|$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


async def _send_reply(message, text: str) -> None:
    """Send a reply as Telegram MarkdownV2, falling back to plain text on failure."""
    try:
        formatted = _to_markdownv2(text)
    except Exception:
        logger.exception("MarkdownV2 conversion failed")
        formatted = None

    if formatted:
        for i in range(0, len(formatted), 4096):
            chunk = formatted[i : i + 4096]
            try:
                await message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception:
                logger.warning("MarkdownV2 send failed, falling back to plain text", exc_info=True)
                plain = _strip_markdown(text)
                for j in range(0, len(plain), 4096):
                    await message.reply_text(plain[j : j + 4096])
                return
    else:
        plain = _strip_markdown(text)
        for j in range(0, len(plain), 4096):
            await message.reply_text(plain[j : j + 4096])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    reply = await agent.chat(update.effective_user.id, "/start")
    await _send_reply(update.message, reply)


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

    await _send_reply(update.message, reply)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo/image messages — download, base64-encode, and send to LLM for OCR/vision.

    This handles both compressed photos (filters.PHOTO) and images sent as
    documents (filters.Document.IMAGE) — e.g. copy-pasted screenshots.
    """
    if not update.message:
        return

    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return
    if _is_rate_limited(user_id):
        await update.message.reply_text("You're sending messages too fast. Please wait a moment.")
        return
    await update.message.chat.send_action("typing")

    try:
        # Determine file_id: from photo array or from document
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        else:
            return

        file = await context.bot.get_file(file_id)
        buf = bytearray()
        await file.download_as_bytearray(buf)

        # Base64-encode for the multimodal LLM call
        img_b64 = base64.b64encode(bytes(buf)).decode("utf-8")

        # Use caption as the user's text prompt, if any
        caption = update.message.caption or ""

        reply = await agent.chat(user_id, caption, image_base64=img_b64)
    except Exception:
        logger.exception("Error processing photo message")
        reply = "Sorry, something went wrong processing your image."

    await _send_reply(update.message, reply)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PDF and other document messages — extract text and send to LLM."""
    if not update.message or not update.message.document:
        return

    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return
    if _is_rate_limited(user_id):
        await update.message.reply_text("You're sending messages too fast. Please wait a moment.")
        return
    await update.message.chat.send_action("typing")

    doc = update.message.document
    mime = doc.mime_type or ""
    caption = update.message.caption or ""

    try:
        file = await context.bot.get_file(doc.file_id)
        buf = bytearray()
        await file.download_as_bytearray(buf)

        if mime == "application/pdf":
            import io
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(bytes(buf)))
            pages_text = []
            for i, page in enumerate(reader.pages, 1):
                text = page.extract_text()
                if text and text.strip():
                    pages_text.append(f"[Page {i}]\n{text.strip()}")

            if not pages_text:
                await update.message.reply_text("I couldn't extract any text from this PDF.")
                return

            extracted = "\n\n".join(pages_text)
            # Truncate if very long to fit in LLM context
            if len(extracted) > 15000:
                extracted = extracted[:15000] + "\n\n... (truncated)"

            prompt = f"The user sent a PDF document"
            if doc.file_name:
                prompt += f' named "{doc.file_name}"'
            prompt += f". Here is the extracted text:\n\n{extracted}"
            if caption:
                prompt += f"\n\nThe user's message: {caption}"
            else:
                prompt += "\n\nPlease summarize the key points of this document."

            reply = await agent.chat(user_id, prompt)
        else:
            reply = f"Sorry, I can't process {mime or 'this'} files yet. I support images and PDFs."
    except ImportError:
        logger.exception("pypdf not installed")
        reply = "Sorry, PDF reading is not available (missing pypdf library)."
    except Exception:
        logger.exception("Error processing document")
        reply = "Sorry, something went wrong processing your document."

    await _send_reply(update.message, reply)


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

    await _send_reply(update.message, reply)


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


DAILY_BRIEFING_PROMPT = (
    "Generate my daily briefing for today. Use your tools to gather REAL data:\n"
    "1. Check my unread emails (use Gmail tools) — highlight urgent ones, group by priority\n"
    "2. Check my calendar for today (use Google Calendar tools) — list all events with times\n"
    "3. Search for the latest news (use web_search) on: AI/Tech, Crypto/Web3, World, and Portugal\n\n"
    "Format the briefing as a clean, concise report with sections for each area. "
    "Include a checklist of action items at the end. "
    "If any tool fails, mention it briefly and continue with the others."
)


async def send_daily_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: generate and send the daily briefing."""
    owner_id_str = os.environ.get("OWNER_USER_ID", "")
    if not owner_id_str:
        logger.warning("OWNER_USER_ID not set — skipping daily briefing")
        return

    owner_id = int(owner_id_str)
    logger.info(f"Generating daily briefing for user {owner_id}")

    try:
        reply = await agent.chat(owner_id, DAILY_BRIEFING_PROMPT)
    except Exception:
        logger.exception("Failed to generate daily briefing")
        reply = "Sorry, I couldn't generate the daily briefing today. Please try manually."

    try:
        formatted = _to_markdownv2(reply)
        for i in range(0, len(formatted), 4096):
            chunk = formatted[i : i + 4096]
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=chunk,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception:
                plain = _strip_markdown(reply)
                for j in range(0, len(plain), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=plain[j : j + 4096])
                return
    except Exception:
        logger.exception("Failed to send daily briefing")
        for i in range(0, len(reply), 4096):
            await context.bot.send_message(chat_id=owner_id, text=reply[i : i + 4096])


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("connect", connect_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 60 seconds
    app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_INTERVAL, first=10)

    # Daily briefing at 08:00 Lisbon time (Europe/Lisbon)
    import pytz
    lisbon_tz = pytz.timezone("Europe/Lisbon")
    if os.environ.get("OWNER_USER_ID"):
        app.job_queue.run_daily(
            send_daily_briefing,
            time=dt_time(hour=8, minute=0, tzinfo=lisbon_tz),
        )
        logger.info("Daily briefing scheduled for 08:00 Europe/Lisbon")
    else:
        logger.warning("OWNER_USER_ID not set — daily briefing disabled")

    logger.info("Eva v2.2 is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
