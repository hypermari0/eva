"""Tool: reveal the tool schemas for a connected external app (Gmail, Calendar, …).

By default Eva exposes zero Composio action schemas to save tokens and reduce
tool-menu confusion. The LLM calls this to opt into an app's tools; they become
available on the NEXT tool round in the same turn.
"""

import composio_bridge

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "load_app_tools",
        "description": (
            "Reveal the concrete tool schemas for a connected external app so you can use them. "
            "Eva's external apps (Gmail, Google Calendar, Slack, GitHub, etc.) are NOT visible "
            "to you by default — call this with the app name and their specific actions become "
            "available on your next tool call. The <external_apps> block in your system prompt "
            "lists which apps are currently connected for this user. Use lowercase names: "
            "'gmail', 'googlecalendar', 'slack', 'github', 'notion', 'googledrive'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Lowercase app name, e.g. 'gmail' or 'googlecalendar'.",
                },
            },
            "required": ["app_name"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    app_name = (args.get("app_name") or "").strip().lower()
    if not app_name:
        return "Error: app_name is required."

    entity_id = str(user_id)
    connected = composio_bridge.get_connected_apps(entity_id)
    if app_name not in connected:
        if connected:
            return (
                f"Error: '{app_name}' is not connected for this user. "
                f"Connected apps: {', '.join(sorted(connected))}. "
                f"Ask the user to run /connect {app_name} first."
            )
        return (
            f"Error: no external apps are connected. "
            f"The user needs to run /connect {app_name} (or similar) first."
        )

    schemas = composio_bridge.get_app_schemas(app_name)
    if not schemas:
        return f"Error: failed to load tool schemas for '{app_name}'."

    composio_bridge.mark_pending_app(entity_id, app_name)
    names = [s["function"]["name"] for s in schemas]
    preview = ", ".join(names[:12])
    if len(names) > 12:
        preview += f", ... (+{len(names) - 12} more)"
    return (
        f"Loaded {len(names)} tools for '{app_name}'. They will be available on your next "
        f"tool call in this turn. Actions include: {preview}"
    )
