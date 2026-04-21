"""Eva — Telegram bot entry point."""

import base64
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta, timezone

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


async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /preferences — show the learned preferences Eva is using."""
    if not _is_allowed(update.effective_user.id):
        return
    try:
        prefs = memory.get_preferences(update.effective_user.id, limit=50)
    except Exception as e:
        await update.message.reply_text(f"Failed to load preferences: {type(e).__name__}: {e}")
        return
    if not prefs:
        await update.message.reply_text(
            "No preferences stored yet. I'll learn as you correct me or state preferences."
        )
        return
    marker = {"positive": "+", "negative": "-", "neutral": "·"}
    lines = ["Learned preferences (latest per topic):", ""]
    for p in prefs:
        topic = p.get("topic", "?")
        sentiment = p.get("sentiment", "neutral")
        preference = (p.get("preference") or "").strip()
        lines.append(f"{marker.get(sentiment, '·')} [{topic}] {preference}")
    await update.message.reply_text("\n".join(lines))


async def _text_to_voice(text: str) -> bytes | None:
    """Convert text to speech via Groq Orpheus TTS. Returns WAV bytes or None."""
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        return None

    # Strip markdown formatting for cleaner speech
    clean = _strip_markdown(text).strip()
    if not clean:
        return None

    # Groq TTS has a 200-char limit per request — split into chunks
    chunks = []
    for sentence in re.split(r"(?<=[.!?])\s+", clean):
        if not sentence.strip():
            continue
        # Further split long sentences
        while len(sentence) > 190:
            cut = sentence[:190].rfind(" ")
            if cut < 50:
                cut = 190
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence.strip():
            chunks.append(sentence.strip())

    if not chunks:
        return None

    audio_parts = []
    async with httpx.AsyncClient(timeout=30) as client:
        for chunk in chunks:
            try:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "playai/playht-tts-v3",
                        "input": chunk,
                        "voice": "Celeste-PlayAI",
                        "response_format": "wav",
                    },
                )
                resp.raise_for_status()
                audio_parts.append(resp.content)
            except Exception:
                logger.warning(f"TTS failed for chunk: {chunk[:50]}...", exc_info=True)

    if not audio_parts:
        return None

    # Concatenate WAV files (skip headers on subsequent parts)
    if len(audio_parts) == 1:
        return audio_parts[0]

    # Simple WAV concatenation: keep first header, append raw data from the rest
    result = bytearray(audio_parts[0])
    for part in audio_parts[1:]:
        # WAV data starts after 44-byte header
        if len(part) > 44:
            result.extend(part[44:])

    # Fix the file size in the WAV header
    total_size = len(result)
    result[4:8] = (total_size - 8).to_bytes(4, "little")
    # Fix data chunk size (at byte 40)
    result[40:44] = (total_size - 44).to_bytes(4, "little")

    return bytes(result)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice/audio messages — transcribe, respond with text + voice."""
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

    # Send text reply
    await _send_reply(update.message, reply)

    # Send voice reply
    try:
        audio = await _text_to_voice(reply)
        if audio:
            await update.message.reply_voice(voice=audio)
    except Exception:
        logger.warning("Failed to send voice reply", exc_info=True)


_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_image_mime(buf: bytes, fallback: str = "image/jpeg") -> str:
    """Detect the real image MIME from magic bytes. Vision models reject mislabeled images."""
    for magic, mime in _IMAGE_MAGIC:
        if buf.startswith(magic):
            return mime
    # WebP: "RIFF....WEBP"
    if len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "image/webp"
    return fallback


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
        # Determine file_id + MIME hint: photos are always JPEG (Telegram compresses),
        # documents carry their own mime_type (PNG screenshots land here).
        doc_mime_hint = "image/jpeg"
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
            if update.message.document.mime_type:
                doc_mime_hint = update.message.document.mime_type
        else:
            return

        file = await context.bot.get_file(file_id)
        buf = bytearray()
        await file.download_as_bytearray(buf)
        raw = bytes(buf)

        # Sniff actual magic bytes — Telegram mime hints are not always reliable,
        # and mislabeling (e.g. PNG sent as image/jpeg) makes strict vision models
        # silently drop the image AND sometimes the caption along with it.
        mime = _sniff_image_mime(raw, fallback=doc_mime_hint)
        img_b64 = base64.b64encode(raw).decode("utf-8")

        caption = update.message.caption or ""
        reply = await agent.chat(user_id, caption, image_base64=img_b64, image_mime=mime)
    except Exception as e:
        logger.exception("Error processing photo message")
        reply = f"Sorry, something went wrong processing your image. ({type(e).__name__}: {e})"

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
    except Exception as e:
        logger.exception("Error processing message")
        reply = f"Sorry, something went wrong. ({type(e).__name__}: {e})"

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


def _build_daily_briefing_prompt() -> str:
    """Build the briefing prompt, optionally appending a local-news region."""
    topics = "AI/Tech, Crypto/Web3, World"
    local_region = os.environ.get("BRIEFING_LOCAL_REGION", "").strip()
    if local_region:
        topics += f", and {local_region}"
    return (
        "Generate my daily briefing for today. Use your tools to gather REAL data — "
        "follow these steps IN ORDER and pass the parameters I specify:\n\n"
        "STEP 0 — Call get_current_datetime FIRST. 'Today' always means today in my local "
        "timezone, never UTC. Note the local date and the local midnight boundaries (start "
        "of today = YYYY-MM-DDT00:00:00±HH:MM, end of today = YYYY-MM-DDT23:59:59±HH:MM). "
        "Gmail and Google Calendar tools are pre-loaded for this briefing — do NOT call "
        "load_app_tools again for those two, their actions are already available.\n\n"
        "STEP 1 — Read my emails with a Gmail fetch/search action.\n"
        "  - Use query: `is:unread newer_than:2d` (focus on urgent, recent unread mail).\n"
        "  - Request at most 20 results.\n"
        "  - For each email, extract: from, subject, date, snippet.\n"
        "  - Group by urgency (urgent / needs-reply / informational), highlight anything "
        "that looks time-sensitive.\n\n"
        "STEP 2 — Read today's calendar with a Google Calendar list-events action.\n"
        "  - Pass timeMin and timeMax in RFC 3339 with my LOCAL timezone offset, covering "
        "today from 00:00 to 23:59 inclusive.\n"
        "  - Use singleEvents=true and orderBy=startTime.\n"
        "  - For each event, report: title, local start–end time, location/link, attendees "
        "(count or names).\n\n"
        f"STEP 3 — web_search the latest news on: {topics}. "
        "3 results max per topic, 1–2 sentence summary each.\n\n"
        "OUTPUT — a concise sectioned report (Emails, Calendar, News, Action items). "
        "If a step errors, say so briefly in that section and continue with the others — "
        "never abandon the whole briefing because one step failed."
    )


async def send_weekly_preferences_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: once per week, list the preferences Eva captured in the last 7 days.

    Sent on Sundays at 09:00 local (see scheduler in main()). Skipped silently when
    zero new preferences were captured — no need to spam an empty digest.
    """
    owner_id_str = os.environ.get("OWNER_USER_ID", "")
    if not owner_id_str:
        return
    owner_id = int(owner_id_str)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    try:
        new_prefs = memory.get_preferences_since(owner_id, since)
    except Exception:
        logger.exception("Weekly preferences digest query failed")
        return
    if not new_prefs:
        return

    marker = {"positive": "+", "negative": "-", "neutral": "·"}
    lines = [
        f"Weekly preferences digest — {len(new_prefs)} captured this week.",
        "Review and /preferences to see the full list. Tell me to drop any that are wrong.",
        "",
    ]
    for p in new_prefs:
        topic = p.get("topic", "?")
        sentiment = p.get("sentiment", "neutral")
        preference = (p.get("preference") or "").strip()
        lines.append(f"{marker.get(sentiment, '·')} [{topic}] {preference}")

    try:
        await context.bot.send_message(chat_id=owner_id, text="\n".join(lines))
    except Exception:
        logger.exception("Failed to send weekly preferences digest")


async def send_daily_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: generate and send the daily briefing."""
    owner_id_str = os.environ.get("OWNER_USER_ID", "")
    if not owner_id_str:
        logger.warning("OWNER_USER_ID not set — skipping daily briefing")
        return

    owner_id = int(owner_id_str)
    logger.info(f"Generating daily briefing for user {owner_id}")

    # Pre-queue Gmail + Calendar for the briefing so the LLM doesn't have to
    # spend a round calling load_app_tools before it can read either one.
    # chat() drains this queue before its first LLM call.
    owner_entity = str(owner_id)
    for app in ("gmail", "googlecalendar"):
        if app in composio_bridge.get_connected_apps(owner_entity):
            composio_bridge.mark_pending_app(owner_entity, app)

    try:
        reply = await agent.chat(owner_id, _build_daily_briefing_prompt())
    except Exception as e:
        logger.exception("Failed to generate daily briefing")
        reply = (
            f"Sorry, I couldn't generate the daily briefing today. "
            f"({type(e).__name__}: {e}). Please try manually."
        )

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
    app.add_handler(CommandHandler("preferences", preferences_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Check for due reminders every 60 seconds
    app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_INTERVAL, first=10)

    # Daily briefing — configure via BRIEFING_TIMEZONE (IANA tz, default UTC)
    # and BRIEFING_HOUR / BRIEFING_MINUTE (24h local time, default 08:00).
    if os.environ.get("OWNER_USER_ID"):
        import pytz
        tz_name = os.environ.get("BRIEFING_TIMEZONE", "UTC")
        try:
            briefing_tz = pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            logger.warning(f"Unknown BRIEFING_TIMEZONE '{tz_name}', falling back to UTC")
            briefing_tz = pytz.UTC
        hour = int(os.environ.get("BRIEFING_HOUR", "8"))
        minute = int(os.environ.get("BRIEFING_MINUTE", "0"))
        app.job_queue.run_daily(
            send_daily_briefing,
            time=dt_time(hour=hour, minute=minute, tzinfo=briefing_tz),
        )
        logger.info(f"Daily briefing scheduled for {hour:02d}:{minute:02d} {tz_name}")

        # Weekly preferences digest — Sundays at 09:00 local.
        app.job_queue.run_daily(
            send_weekly_preferences_digest,
            time=dt_time(hour=9, minute=0, tzinfo=briefing_tz),
            days=(6,),
        )
        logger.info(f"Weekly preferences digest scheduled for Sun 09:00 {tz_name}")
    else:
        logger.info("OWNER_USER_ID not set — daily briefing and weekly digest disabled")

    logger.info("Eva is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
