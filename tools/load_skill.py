"""Tool: reveal a dynamic skill's full tool schema for use in this turn."""

import tools


TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Reveal the full tool schema for a dynamic skill so you can call it. Dynamic skills "
            "are NOT visible to you by default — the system prompt only shows their names and "
            "short descriptions (see the `<dynamic_skills>` block). Call this with the skill "
            "name and its parameter schema becomes available on your next tool call IN THIS "
            "TURN. You only need to do this once per turn per skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The dynamic skill's snake_case name.",
                },
            },
            "required": ["name"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    name = (args.get("name") or "").strip()
    if not name:
        return "Error: name is required."

    if name not in tools.DYNAMIC_SKILL_REGISTRY:
        available = sorted(tools.DYNAMIC_SKILL_REGISTRY)
        if not available:
            return "Error: no dynamic skills are registered."
        preview = ", ".join(available[:12])
        if len(available) > 12:
            preview += f", … (+{len(available) - 12} more)"
        return f"Error: no skill named '{name}'. Available: {preview}"

    tools.mark_pending_skill(user_id, name)
    schema = tools.DYNAMIC_SKILL_REGISTRY[name]
    params = (schema.get("function") or {}).get("parameters") or {}
    required = params.get("required") or []
    props = list((params.get("properties") or {}).keys())
    return (
        f"Loaded skill '{name}'. It will be available on your next tool call in this turn.\n"
        f"Parameters: {props} (required: {required})"
    )
