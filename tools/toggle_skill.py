"""Tool: enable or disable a dynamic skill."""

import memory
import tools

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "toggle_skill",
        "description": "Enable or disable a dynamic skill by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name to toggle.",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "True to enable, false to disable.",
                },
            },
            "required": ["name", "enabled"],
        },
    },
}


def run(args: dict) -> str:
    name = args["name"]
    enabled = args["enabled"]

    if name not in tools.DYNAMIC_SKILLS and name not in [s["name"] for s in memory.list_dynamic_skills()]:
        return f"Error: no dynamic skill named '{name}' found."

    memory.update_dynamic_skill(name, enabled=enabled)

    if enabled:
        # Re-load from DB to get full data
        all_skills = memory.load_dynamic_skills()
        skill = next((s for s in all_skills if s["name"] == name), None)
        if skill:
            tools.register_dynamic_skill(
                name=skill["name"],
                description=skill["description"],
                parameters=skill["parameters"],
                code=skill["code"],
            )
        return f"Skill '{name}' enabled."
    else:
        tools.unregister_dynamic_skill(name)
        return f"Skill '{name}' disabled."
