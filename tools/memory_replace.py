"""Tool: replace an entry in the user's curated memory notebook (substring match)."""

import memory

MEMORY_CAP = 2200


TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_replace",
        "description": (
            "Find the FIRST entry in the user's memory notebook that contains `match` "
            "(case-insensitive substring) and replace it with `new_entry`. Use when you need "
            "to update something you previously saved — e.g. the user moved cities, changed "
            "work hours, or an old convention no longer applies. If nothing matches, nothing "
            "is written."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "match": {
                    "type": "string",
                    "description": "Substring to find in an existing entry (case-insensitive).",
                },
                "new_entry": {
                    "type": "string",
                    "description": "The replacement entry text. No newlines.",
                },
            },
            "required": ["match", "new_entry"],
        },
    },
}


def run(args: dict) -> str:
    user_id = args.get("_user_id")
    if not user_id:
        return "Error: no user context available."
    match = (args.get("match") or "").strip()
    new_entry = (args.get("new_entry") or "").strip().replace("\n", " ")
    if not match:
        return "Error: match is required."
    if not new_entry:
        return "Error: new_entry is empty."
    if len(new_entry) > 200:
        new_entry = new_entry[:200].rstrip()

    body = memory.get_user_memory(user_id)
    lines = [ln for ln in body.split("\n") if ln.strip()]

    m_lower = match.lower()
    replaced = False
    out_lines = []
    for ln in lines:
        if not replaced and m_lower in ln.lower():
            out_lines.append(new_entry)
            replaced = True
        else:
            out_lines.append(ln)

    if not replaced:
        return f"No entry contained '{match}'. Nothing changed."

    new_body = "\n".join(out_lines)
    if len(new_body) > MEMORY_CAP:
        return (
            f"Error: replacement would exceed the {MEMORY_CAP}-char cap "
            f"({len(new_body)} chars). Trim `new_entry` or remove another line first."
        )
    memory.save_user_memory(user_id, new_body)
    return f"Memory entry replaced ({len(out_lines)} entries, {len(new_body)}/{MEMORY_CAP} chars)."
