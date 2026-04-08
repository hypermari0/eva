"""Tool: propose a new dynamic skill (requires user approval to activate)."""

import json
import re

import memory
import tools

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": (
            "Propose a new skill/tool. The skill will NOT activate immediately — "
            "the user must approve it first via /approve command. "
            "After calling this, explain to the user what the skill will do, "
            "what external APIs it calls, and tell them to send /approve <name> to activate it. "
            "The code runs in a sandbox with access to: json, math, datetime, re, urllib.parse, httpx. "
            "Use httpx for any HTTP calls. "
            "The code should be the body of a function that receives 'args' dict and returns a string."
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
                "summary": {
                    "type": "string",
                    "description": "A short plain-language summary of what this skill does and what external services/APIs it connects to. This is shown to the user for approval.",
                },
            },
            "required": ["name", "description", "parameters", "code", "summary"],
        },
    },
}

RESERVED = {"create_skill", "list_skills", "toggle_skill", "delete_skill",
            "get_current_datetime", "web_search", "create_reminder", "update_user_profile"}


def run(args: dict) -> str:
    name = args["name"]
    user_id = args.get("_user_id")

    if not re.match(r"^[a-z][a-z0-9_]{1,48}$", name):
        return "Error: name must be snake_case, start with a letter, 2-49 chars."

    if name in RESERVED:
        return f"Error: '{name}' is a reserved tool name."

    if name in tools.TOOLS and name not in tools.DYNAMIC_SKILLS:
        return f"Error: '{name}' conflicts with a built-in tool."

    # Syntax check + security validation
    code = args["code"]
    wrapped = "def _test(args):\n"
    for line in code.split("\n"):
        wrapped += f"    {line}\n"
    try:
        compile(wrapped, "<dynamic-skill>", "exec")
    except SyntaxError as e:
        return f"Syntax error in code: {e}"

    try:
        from tools import _validate_code_ast
        _validate_code_ast(code)
    except ValueError as e:
        return f"Security validation failed: {e}"

    # Save as pending
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

    summary = args.get("summary", args["description"])
    return (
        f"Skill '{name}' saved as PENDING (not active yet).\n\n"
        f"Summary: {summary}\n\n"
        f"Tell the user to send /approve {name} to activate it, "
        f"or /reject {name} to discard it."
    )
