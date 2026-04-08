"""Tool: change the LLM model Eva uses via OpenRouter."""

import memory

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "change_model",
        "description": (
            "Change the AI model that Eva uses. The model must be available on OpenRouter. "
            "Examples: 'anthropic/claude-sonnet-4-5', 'mistralai/mistral-small-2603', "
            "'google/gemma-4-31b-it', 'openai/gpt-4o'. "
            "The change takes effect on the next message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "The OpenRouter model ID (e.g. 'mistralai/mistral-small-2603').",
                },
            },
            "required": ["model"],
        },
    },
}


def run(args: dict) -> str:
    model = args["model"].strip()
    if not model or "/" not in model:
        return "Error: model must be in 'provider/model-name' format (e.g. 'mistralai/mistral-small-2603')."

    old_model = memory.get_setting("model", "unknown")
    memory.set_setting("model", model)
    return f"Model changed from '{old_model}' to '{model}'. Takes effect on the next message."
