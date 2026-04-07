"""LLM agent: sends messages to OpenRouter, handles tool call loops."""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from tools import TOOLS, RUNNERS
import memory

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "qwen/qwen3.6-plus:free"
MAX_TOOL_ROUNDS = 5

_soul: str | None = None
_prompt_template: str | None = None


def _load_templates() -> None:
    global _soul, _prompt_template
    base = Path(__file__).parent / "config"
    _soul = (base / "soul.md").read_text().strip()
    _prompt_template = (base / "system_prompt.txt").read_text().strip()


def build_system_prompt(user_id: int) -> str:
    if _soul is None or _prompt_template is None:
        _load_templates()

    profile = memory.get_user_profile(user_id)
    if profile:
        user_section = profile
    else:
        user_section = (
            "No profile yet — this is a new user. "
            "Start by warmly introducing yourself and asking who they are: "
            "their name, what they do, and what they'd like help with. "
            "Save what you learn with update_user_profile."
        )

    return _prompt_template.replace("{{SOUL}}", _soul).replace("{{USER_PROFILE}}", user_section)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    }


def _build_messages(user_id: int, user_text: str) -> list[dict]:
    history = memory.load_history(user_id)
    messages = [{"role": "system", "content": build_system_prompt(user_id)}]
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
        for attempt in range(4):
            resp = await client.post(OPENROUTER_URL, headers=_headers(), json=payload)
            if resp.status_code == 429:
                wait = 2 ** attempt  # 1s, 2s, 4s, 8s
                logger.warning(f"Rate limited (429), retrying in {wait}s...")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        # Final attempt — let it raise if still 429
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
