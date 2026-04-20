"""Tool: returns the current date, time, and day of week."""

import os
from datetime import datetime, timezone

import pytz

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": (
            "Returns the current date, time, and day of the week in BOTH the user's local "
            "timezone AND UTC. Always call this before scheduling calendar events or reminders "
            "so you have an accurate timezone anchor — the user speaks in local time."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def _user_tz():
    tz_name = (
        os.environ.get("USER_TIMEZONE")
        or os.environ.get("BRIEFING_TIMEZONE")
        or "UTC"
    )
    try:
        return pytz.timezone(tz_name), tz_name
    except pytz.UnknownTimeZoneError:
        return pytz.UTC, "UTC"


def run(_args: dict) -> str:
    utc_now = datetime.now(timezone.utc)
    tz, tz_name = _user_tz()
    local_now = utc_now.astimezone(tz)
    offset = local_now.strftime("%z")
    offset_fmt = f"{offset[:3]}:{offset[3:]}" if offset else ""
    return (
        f"Local time ({tz_name}): {local_now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"{offset_fmt} ({local_now.strftime('%A')})\n"
        f"UTC: {utc_now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"ISO 8601 local: {local_now.strftime('%Y-%m-%dT%H:%M:%S%z')}"
    )
