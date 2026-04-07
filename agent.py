"""LLM agent: sends messages to OpenRouter, handles tool call loops."""

import json
import logging
import os
from pathlib import Path

import httpx

from tools import TOOLS, RUNNERS
import memory

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-4-5"
MAX_TOOL_ROUNDS = 5

_system_prompt: str | None = None


def get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        path = Path(__file__).parent / "config" / "system_prompt.txt"
        _system_prompt = path.read_text().strip()
    return _system_prompt


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    }


def _build_messages(user_id: int, user_text: str) -> list[dict]:
    history = memory.load_history(user_id)
    messages = [{"role": "system", "content": get_system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    return messages


async def _call_llm(messages: list[dict], tools: list[dict]) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": tools or None,
        "max_tokens": 4096,
    }
    # Remove tools key entirely if empty to avoid API issues
    if not tools:
        payload.pop("tools", None)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(OPENROUTER_URL, headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()


async def chat(user_id: int, user_text: str) -> str:
    """Process a user message: load history, call LLM with tool loop, return final text."""
    # Save user message
    memory.save_message(user_id, "user", user_text)

    messages = _build_messages(user_id, user_text)
    tool_list = list(TOOLS.values())

    for _ in range(MAX_TOOL_ROUNDS):
        data = await _call_llm(messages, tool_list)
        choice = data["choices"][0]
        msg = choice["message"]

        # No tool calls — we have the final answer
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            text = msg.get("content", "")
            memory.save_message(user_id, "assistant", text)
            return text

        # Append assistant message with tool calls
        messages.append(msg)

        # Execute each tool call
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            runner = RUNNERS.get(fn_name)
            if runner is None:
                result = f"Error: unknown tool '{fn_name}'"
            else:
                try:
                    args["_user_id"] = user_id
                    result = runner(args)
                except Exception as e:
                    logger.exception(f"Tool {fn_name} failed")
                    result = f"Error running {fn_name}: {e}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    # If we exhausted tool rounds, do one final call without tools
    data = await _call_llm(messages, [])
    text = data["choices"][0]["message"].get("content", "")
    memory.save_message(user_id, "assistant", text)
    return text
