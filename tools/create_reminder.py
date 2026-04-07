"""Tool: create a reminder stored in Supabase."""

import memory

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "create_reminder",
        "description": "Create a reminder for the user. The remind_at time must be in ISO 8601 format (e.g. 2025-01-15T14:30:00Z). Use the get_current_datetime tool first if you need to know the current time to calculate a relative reminder.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to remind the user about.",
                },
                "remind_at": {
                    "type": "string",
                    "description": "When to send the reminder, in ISO 8601 format with timezone.",
                },
            },
            "required": ["message", "remind_at"],
        },
    },
}

# user_id is injected by the agent before calling run()
def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    reminder = memory.create_reminder(
        user_id=user_id,
        message=args["message"],
        remind_at=args["remind_at"],
    )
    return f"Reminder created (id={reminder['id']}). I'll remind you: \"{args['message']}\" at {args['remind_at']}."
