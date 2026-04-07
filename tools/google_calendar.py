"""Tool: Google Calendar — read and create events via service account."""

import json
import os
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "google_calendar",
        "description": (
            "Interact with the user's Google Calendar. "
            "Actions: 'list' to get upcoming events, 'create' to add a new event. "
            "For 'list': provide start_date and optionally end_date (ISO 8601 date, e.g. 2025-01-15). "
            "Defaults to the next 7 days. "
            "For 'create': provide title, start_time, end_time (ISO 8601 datetime, e.g. 2025-01-15T14:00:00), "
            "and optionally description and location."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create"],
                    "description": "The action to perform.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date for listing events (ISO 8601 date). Defaults to today.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date for listing events (ISO 8601 date). Defaults to 7 days from start.",
                },
                "title": {
                    "type": "string",
                    "description": "Event title (for create).",
                },
                "start_time": {
                    "type": "string",
                    "description": "Event start datetime in ISO 8601 (for create).",
                },
                "end_time": {
                    "type": "string",
                    "description": "Event end datetime in ISO 8601 (for create).",
                },
                "description": {
                    "type": "string",
                    "description": "Event description (for create, optional).",
                },
                "location": {
                    "type": "string",
                    "description": "Event location (for create, optional).",
                },
            },
            "required": ["action"],
        },
    },
}

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")

    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds)


def _get_calendar_id():
    return os.environ.get("GOOGLE_CALENDAR_ID", "primary")


def _list_events(args: dict) -> str:
    service = _get_service()
    calendar_id = _get_calendar_id()

    now = datetime.now(timezone.utc)
    start_date = args.get("start_date")
    if start_date:
        time_min = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    else:
        time_min = now

    end_date = args.get("end_date")
    if end_date:
        time_max = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    else:
        time_max = time_min + timedelta(days=7)

    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=30,
        )
        .execute()
    )

    events = events_result.get("items", [])
    if not events:
        return "No events found for this period."

    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        end = e["end"].get("dateTime", e["end"].get("date"))
        summary = e.get("summary", "(no title)")
        location = e.get("location", "")
        line = f"- **{summary}** | {start} → {end}"
        if location:
            line += f" | {location}"
        lines.append(line)

    return "\n".join(lines)


def _create_event(args: dict) -> str:
    service = _get_service()
    calendar_id = _get_calendar_id()

    title = args.get("title")
    start_time = args.get("start_time")
    end_time = args.get("end_time")

    if not title or not start_time or not end_time:
        return "Error: 'title', 'start_time', and 'end_time' are required to create an event."

    event_body = {
        "summary": title,
        "start": {"dateTime": start_time, "timeZone": "Europe/Lisbon"},
        "end": {"dateTime": end_time, "timeZone": "Europe/Lisbon"},
    }

    if args.get("description"):
        event_body["description"] = args["description"]
    if args.get("location"):
        event_body["location"] = args["location"]

    event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
    return f"Event created: **{event.get('summary')}** on {start_time} → {end_time} (ID: {event['id']})"


def run(args: dict) -> str:
    action = args.get("action")
    if action == "list":
        return _list_events(args)
    elif action == "create":
        return _create_event(args)
    else:
        return f"Unknown action: {action}. Use 'list' or 'create'."
