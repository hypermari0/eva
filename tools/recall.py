"""Tool: full-text search the user's older conversation history.

Eva injects the top FTS hits automatically at the start of each turn, but the
LLM can also trigger a targeted search when it needs more context than the
auto-prefetch pulled in (different keywords, higher limit, etc.).
"""

import memory

MAX_HITS = 5
HIT_CHAR_CAP = 400

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "recall",
        "description": (
            "Search the user's older conversation history (everything, not just the recent 20 "
            "messages) with full-text search. Use this when the user references something from "
            "the past ('what did we decide about X', 'remember when I said Y') and you don't "
            "see it in the current message window. Returns the best matches with their dates. "
            f"Returns at most {MAX_HITS} hits, each truncated to {HIT_CHAR_CAP} chars."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query in websearch syntax — supports quoted phrases, OR, -minus. "
                        "Example: 'tokyo trip OR flight', '\"project roadmap\"', 'budget -2024'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max hits to return (default 3, cap {MAX_HITS}).",
                },
            },
            "required": ["query"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    query = (args.get("query") or "").strip()
    if len(query) < 3:
        return "Error: query must be at least 3 characters."
    limit = args.get("limit") or 3
    try:
        limit = max(1, min(int(limit), MAX_HITS))
    except (TypeError, ValueError):
        limit = 3
    hits = memory.search_messages(user_id, query, limit=limit)
    if not hits:
        return f"No older messages matched '{query}'."
    lines = []
    for h in hits:
        ts = (h.get("created_at") or "")[:10]
        role = h.get("role", "?")
        content = (h.get("content") or "").strip()
        if len(content) > HIT_CHAR_CAP:
            content = content[:HIT_CHAR_CAP].rstrip() + "…"
        lines.append(f"[{ts}] {role}: {content}")
    return "\n\n".join(lines)
