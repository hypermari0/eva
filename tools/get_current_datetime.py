"""Tool: returns the current date, time, and day of week."""

from datetime import datetime, timezone

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": "Returns the current date, time (UTC), and day of the week.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def run(_args: dict) -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC (%A)")
