"""Tool: retire a previously-recorded preference by topic."""

import memory

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "forget_preference",
        "description": (
            "Delete all stored preferences for a given topic. Use when the user explicitly "
            "says 'forget that', 'scrap that rule', 'I changed my mind about X', or similar. "
            "If the user is merely updating a preference, call `record_preference` instead "
            "(latest entry wins); only call `forget_preference` when they want the topic wiped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The topic key to forget (same snake_case key used with record_preference).",
                },
            },
            "required": ["topic"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    topic = (args.get("topic") or "").strip().lower()
    if not topic:
        return "Error: topic is required."
    try:
        deleted = memory.forget_preference(user_id=user_id, topic=topic)
    except Exception as e:
        return f"Error deleting preference: {type(e).__name__}: {e}"
    if deleted == 0:
        return f"No preference found for topic '{topic}'."
    return f"Forgot {deleted} entr{'y' if deleted == 1 else 'ies'} for topic '{topic}'."
