"""Tool: append an entry to the user's curated memory notebook."""

import memory

MEMORY_CAP = 2200


TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_add",
        "description": (
            "Append one short line to the user's curated memory notebook. Use this for durable "
            "FACTS about the user's environment, conventions, or lessons you've learned — e.g. "
            "\"user runs Eva from a M2 Mac in Lisbon\", \"prefers concise briefings in the morning\", "
            "\"avoid scheduling meetings before 10:00 local\". Keep each entry < 160 chars. Do NOT "
            "use this for preferences about how you should behave — use `record_preference` for those. "
            "Do NOT use this for biographical profile facts — use `update_user_profile` for those. "
            f"The notebook has a hard {MEMORY_CAP}-char cap; if you hit it, use `memory_replace` or "
            "`memory_remove` to prune first. The notebook is injected into your system prompt every "
            "turn, so curate ruthlessly — quality over quantity."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entry": {
                    "type": "string",
                    "description": "A single short line to append. No newlines.",
                },
            },
            "required": ["entry"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    entry = (args.get("entry") or "").strip().replace("\n", " ")
    if not entry:
        return "Error: entry is empty."
    if len(entry) > 200:
        entry = entry[:200].rstrip()

    body = memory.get_user_memory(user_id)
    lines = [ln for ln in body.split("\n") if ln.strip()]

    # dedupe: case-insensitive identical lines
    if any(ln.strip().lower() == entry.lower() for ln in lines):
        return "Entry already present — not added."

    lines.append(entry)
    new_body = "\n".join(lines)
    if len(new_body) > MEMORY_CAP:
        return (
            f"Error: adding this would exceed the {MEMORY_CAP}-char memory cap "
            f"(current {len(body)}, after add {len(new_body)}). "
            "Use `memory_remove` to prune an older entry first."
        )
    memory.save_user_memory(user_id, new_body)
    return f"Memory updated ({len(lines)} entries, {len(new_body)}/{MEMORY_CAP} chars)."
