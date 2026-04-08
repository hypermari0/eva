"""Tool: permanently delete a dynamic skill."""

import memory
import tools

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "delete_skill",
        "description": "Permanently delete a dynamic skill by name. This cannot be undone.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name to delete.",
                },
            },
            "required": ["name"],
        },
    },
}


def run(args: dict) -> str:
    name = args["name"]

    if name not in tools.DYNAMIC_SKILLS and name not in [s["name"] for s in memory.list_dynamic_skills()]:
        return f"Error: no dynamic skill named '{name}' found."

    tools.unregister_dynamic_skill(name)
    deleted = memory.delete_dynamic_skill(name)

    if deleted:
        return f"Skill '{name}' deleted permanently."
    return f"Error: could not delete '{name}'."
