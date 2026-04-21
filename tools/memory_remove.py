"""Tool: remove entries from the user's curated memory notebook (substring match)."""

import memory

MEMORY_CAP = 2200


TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_remove",
        "description": (
            "Remove every entry in the user's memory notebook that contains `match` "
            "(case-insensitive substring). Use when a note is wrong, obsolete, or the user "
            "explicitly asks you to forget something. Returns the number of entries removed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "match": {
                    "type": "string",
                    "description": "Substring to search for (case-insensitive). All matching entries are removed.",
                },
            },
            "required": ["match"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    match = (args.get("match") or "").strip()
    if not match:
        return "Error: match is required."

    body = memory.get_user_memory(user_id)
    lines = [ln for ln in body.split("\n") if ln.strip()]

    m_lower = match.lower()
    kept = [ln for ln in lines if m_lower not in ln.lower()]
    removed = len(lines) - len(kept)

    if removed == 0:
        return f"No entries contained '{match}'. Nothing removed."

    memory.save_user_memory(user_id, "\n".join(kept))
    return f"Removed {removed} entr{'y' if removed == 1 else 'ies'} ({len(kept)} remaining)."
