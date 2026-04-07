"""Tool: update what Eva knows about the user."""

import memory

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "update_user_profile",
        "description": "Update your stored knowledge about this user. Call this whenever you learn something new about them — their name, role, interests, preferences, goals, or anything that would help you be a better assistant in future conversations. Pass the FULL updated profile each time (not just the new info), as this overwrites the previous version.",
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "The complete user profile in markdown format. Include everything you know: name, role, interests, preferences, context, etc.",
                },
            },
            "required": ["profile"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    memory.save_user_profile(user_id, args["profile"])
    return "User profile updated."
