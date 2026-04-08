"""Tool: list all dynamic skills."""

import memory

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "list_skills",
        "description": "List all dynamic skills that have been created, showing their name, description, and enabled status.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def run(_args: dict) -> str:
    skills = memory.list_dynamic_skills()
    if not skills:
        return "No dynamic skills have been created yet."

    lines = []
    for s in skills:
        status = "enabled" if s["enabled"] else "disabled"
        lines.append(f"- **{s['name']}** ({status}): {s['description']}")
    return "\n".join(lines)
