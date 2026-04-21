"""Tool: record a durable preference the user has expressed about how Eva should behave.

Eva calls this when the user corrects her, praises an approach, or states a
clear directive about future behavior. Single topic per call — latest row per
topic wins (older ones aren't deleted, just shadowed on read).
"""

import re

import memory

MAX_PREFERENCE_CHARS = 500
MAX_EXCERPT_CHARS = 280

# Tight topic validator: lowercase alphanumeric + underscore + hyphen. Blocks
# freeform injection into the fenced preferences block downstream.
_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,39}$")

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "record_preference",
        "description": (
            "Record a durable preference about how you should behave in the future. Call this "
            "when the user CORRECTS you ('don't do X'), PRAISES a specific approach ('yes, like "
            "that'), or states a clear directive ('from now on, always Y'). Do NOT call it for "
            "one-off factual answers or casual agreement. Keep one preference per call; call "
            "again for a second preference. The latest preference per topic overrides older ones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "Short snake-case key for the kind of preference. Reuse existing topics "
                        "when updating the same area. Examples: 'tone', 'reply_length', "
                        "'briefing_time', 'language', 'emoji_use', 'formality', 'reminders', "
                        "'summaries', 'privacy'. Lowercase, 2–40 chars, a-z 0-9 _ -."
                    ),
                },
                "preference": {
                    "type": "string",
                    "description": (
                        "The preference itself in one or two sentences, phrased as a durable rule "
                        "Eva should follow. Example: 'Keep replies to 2 sentences unless I ask "
                        "for detail.'"
                    ),
                },
                "sentiment": {
                    "type": "string",
                    "enum": ["positive", "negative", "neutral"],
                    "description": (
                        "'positive' = user liked this and wants more of it; 'negative' = user "
                        "disliked this and wants less of it; 'neutral' = stated fact/preference."
                    ),
                },
                "source_excerpt": {
                    "type": "string",
                    "description": (
                        "Short quote from the user's message that prompted this (≤280 chars). "
                        "Helps Mario audit the stored preference later."
                    ),
                },
            },
            "required": ["topic", "preference", "sentiment"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    topic = (args.get("topic") or "").strip().lower()
    preference = (args.get("preference") or "").strip()
    sentiment = (args.get("sentiment") or "neutral").strip().lower()
    source = (args.get("source_excerpt") or "").strip() or None

    if not _TOPIC_RE.match(topic):
        return (
            "Error: topic must be lowercase snake_case, 2–40 chars, a-z 0-9 _ -. "
            f"Got: {topic!r}"
        )
    if not preference:
        return "Error: preference cannot be empty."
    if len(preference) > MAX_PREFERENCE_CHARS:
        preference = preference[:MAX_PREFERENCE_CHARS].rstrip() + "…"
    if sentiment not in ("positive", "negative", "neutral"):
        sentiment = "neutral"
    if source and len(source) > MAX_EXCERPT_CHARS:
        source = source[:MAX_EXCERPT_CHARS].rstrip() + "…"

    try:
        memory.record_preference(
            user_id=user_id,
            topic=topic,
            preference=preference,
            sentiment=sentiment,
            source_excerpt=source,
        )
    except Exception as e:
        return f"Error saving preference: {type(e).__name__}: {e}"
    return f"Preference recorded under topic '{topic}' ({sentiment})."
