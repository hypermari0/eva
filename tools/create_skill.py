"""Tool: create a new dynamic skill that becomes immediately available."""

import re

import memory
import tools

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": (
            "Create a new skill/tool that you can use in future conversations. "
            "The code runs in a sandbox with access to: json, math, datetime, re, urllib.parse, httpx. "
            "Use httpx for any HTTP calls (not requests). "
            "The code should be the body of a function that receives 'args' dict and returns a string. "
            "Example code: 'import httpx\\nresp = httpx.get(\"https://api.example.com/data\")\\nreturn resp.json()[\"result\"]'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Snake_case function name (e.g. check_btc_price). Must be unique.",
                },
                "description": {
                    "type": "string",
                    "description": "What this skill does — you'll read this to decide when to use it.",
                },
                "parameters": {
                    "type": "object",
                    "description": "JSON Schema for the skill's parameters. Use {\"type\": \"object\", \"properties\": {}, \"required\": []} for zero-arg skills.",
                },
                "code": {
                    "type": "string",
                    "description": "Python code for the run function body. Receives 'args' dict. Must return a string.",
                },
            },
            "required": ["name", "description", "parameters", "code"],
        },
    },
}

# Static tool names that cannot be overwritten
RESERVED = {"create_skill", "list_skills", "toggle_skill", "delete_skill",
            "get_current_datetime", "web_search", "create_reminder", "update_user_profile"}


def run(args: dict) -> str:
    name = args["name"]
    user_id = args.get("_user_id")

    # Validate name
    if not re.match(r"^[a-z][a-z0-9_]{1,48}$", name):
        return "Error: name must be snake_case, start with a letter, 2-49 chars."

    if name in RESERVED:
        return f"Error: '{name}' is a reserved tool name."

    if name in tools.TOOLS and name not in tools.DYNAMIC_SKILLS:
        return f"Error: '{name}' conflicts with a built-in tool."

    # Syntax check
    code = args["code"]
    wrapped = "def _test(args):\n"
    for line in code.split("\n"):
        wrapped += f"    {line}\n"
    try:
        compile(wrapped, "<dynamic-skill>", "exec")
    except SyntaxError as e:
        return f"Syntax error in code: {e}"

    # Save to Supabase
    try:
        memory.create_dynamic_skill(
            name=name,
            description=args["description"],
            parameters=args["parameters"],
            code=code,
            created_by=user_id,
        )
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            return f"Error: a skill named '{name}' already exists. Delete it first or choose another name."
        return f"Error saving skill: {e}"

    # Register in-memory
    tools.register_dynamic_skill(
        name=name,
        description=args["description"],
        parameters=args["parameters"],
        code=code,
    )

    return f"Skill '{name}' created and ready to use."
