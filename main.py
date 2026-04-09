"""Eva — Telegram bot entry point."""

import base64
import logging
import os
import re
import time
from collections import defaultdict

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


def _md_to_html(text: str) -> str:
    """Convert common Markdown from LLM output to Telegram-compatible HTML.

    Handles: **bold**, *italic*, `inline code`, ```code blocks```, [links](url),
    ### headings (→ bold), --- (→ removed), markdown tables (→ clean lines).
    """
    import html as html_mod

    # Step 1 — protect code blocks/inline code before escaping HTML entities
    code_blocks: list[str] = []

    def _stash_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    inline_codes: list[str] = []

    def _stash_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    # Stash code blocks first (```...```), then inline (`...`)
    text = re.sub(r"```(?:\w*\n)?(.*?)```", _stash_code_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", _stash_inline_code, text)

    # Step 2 — handle Markdown structure before HTML-escaping
    # Remove horizontal rules
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)

    # Convert ### headings to bold (Telegram has no heading support)
    def _heading_to_bold(m: re.Match) -> str:
        content = m.group(1).strip()
        # Strip existing ** wrapping to avoid double-bold
        if content.startswith("**") and content.endswith("**"):
            content = content[2:-2]
        return f"**{content}**"

    text = re.sub(r"^#{1,6}\s+(.+)$", _heading_to_bold, text, flags=re.MULTILINE)

    # Convert markdown tables: remove separator rows, strip leading/trailing pipes
    text = re.sub(r"^\|[-\s|:]+\|$", "", text, flags=re.MULTILINE)  # separator rows
    text = re.sub(r"^\|\s*(.+?)\s*\|$", r"\1", text, flags=re.MULTILINE)  # data rows

    # Step 3 — HTML-escape the rest (only <, >, & matter for Telegram HTML)
    text = html_mod.escape(text)

    # Step 4 — convert Markdown formatting to HTML tags
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # Step 5 — restore code blocks and inline code with HTML tags
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", f"<code>{html_mod.escape(code)}</code>")
    for i, code in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{html_mod.escape(code)}</pre>")

    # Clean up excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


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
    """Send a reply as Telegram HTML, falling back to stripped plain text on failure."""
    try:
        html_text = _md_to_html(text)
    except Exception:
        logger.exception("_md_to_html conversion failed")
        html_text = None

    if html_text:
        for i in range(0, len(html_text), 4096):
            chunk = html_text[i : i + 4096]
            try:
                await message.reply_text(chunk, parse_mode=ParseMode.HTML)
            except Exception:
                logger.warning("HTML send failed for chunk, falling back to plain text", exc_info=True)
                plain = _strip_markdown(text)
                for j in range(0, len(plain), 4096):
                    await message.reply_text(plain[j : j + 4096])
                return
    else:
        # Conversion failed entirely — send stripped plain text
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


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("connect", connect_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 60 seconds
    app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_INTERVAL, first=10)

    logger.info("Eva v2.1 is running (HTML formatting, OCR, Composio logging).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
