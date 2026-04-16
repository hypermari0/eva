# Eva

**A personal AI assistant that lives in Telegram.**

Eva is a Telegram bot powered by a frontier LLM (via OpenRouter), with persistent memory (Supabase), multimodal input (text, voice, images, PDFs), external app integrations (Google Calendar, Gmail, GitHub, Slack, … via Composio), and a tool system that can extend itself at runtime.

She's designed to be *ambient* instead of *destination* AI: no new app to open, no new tab to switch to — just a contact you chat with like any other.

## Features

- **Chat with any OpenRouter model** — Claude, GPT, Gemini, Mistral, Llama. Switch at runtime.
- **Persistent memory** — conversation history + per-user profile, persisted in Supabase.
- **Multimodal** — handles text, voice messages (transcribed + voice reply), photos (OCR / vision), and PDFs (extracted + summarized).
- **Reminders** — natural-language reminders delivered via Telegram.
- **Web search** — via Tavily.
- **External apps** — Google Calendar, Gmail, Slack, GitHub and more via Composio OAuth.
- **Daily briefing** — optional scheduled briefing at a time and timezone you configure.
- **Self-extending** — the LLM can propose new tools at runtime. Tools go through an approval flow before they activate, and run in a restricted AST-validated sandbox.
- **Access controlled** — allowlist Telegram user IDs so only you (and whoever you choose) can message your instance.
- **Deploy-ready** — runs locally with `python main.py`, or one-click deploy to Railway.

## Quick start

### 1. Prerequisites

You'll need accounts and API keys for:

- **Telegram** — create a bot via [@BotFather](https://t.me/BotFather) and grab the token.
- **OpenRouter** — [openrouter.ai](https://openrouter.ai) → API key. Pay as you go.
- **Supabase** — [supabase.com](https://supabase.com) → new project → **service_role** key (not anon).

Optional but recommended:

- **Tavily** ([tavily.com](https://tavily.com)) — web search.
- **Groq** ([groq.com](https://console.groq.com)) — voice transcription + TTS.
- **Composio** ([composio.dev](https://composio.dev)) — Google Calendar, Gmail, Slack, GitHub, etc.

### 2. Set up the database

In the Supabase SQL editor, paste and run the contents of [`supabase/schema.sql`](supabase/schema.sql). It creates the tables and enables row-level security.

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your keys. At minimum:
#   TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, SUPABASE_URL, SUPABASE_KEY
# Strongly recommended: set ALLOWED_USER_IDS to your own Telegram ID
# so strangers can't burn through your API credits.
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### 4. Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Open Telegram, message your bot, and say hi.

### 5. (Optional) Deploy to Railway

1. Push the repo to your own GitHub.
2. [railway.app](https://railway.app) → **New project** → **Deploy from GitHub repo**.
3. Add your env vars in **Variables**.
4. Railway picks up `railway.toml` and runs `python main.py`. Done.

## Extending Eva

### Add a static tool

Drop a file in `tools/`. Two exports and you're done:

```python
# tools/my_tool.py
TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "What this tool does — the LLM reads this to decide when to use it.",
        "parameters": {
            "type": "object",
            "properties": {
                "arg_name": {"type": "string", "description": "What this argument is for."},
            },
            "required": ["arg_name"],
        },
    },
}


def run(args: dict) -> str:
    # args["_user_id"] is always injected (Telegram user ID)
    return f"Result for {args['arg_name']}"
```

Restart the bot. The tool is auto-discovered and available to the LLM immediately.

### Let Eva add her own tools

Ask her to. Example:

> *"Can you check the weather for me?"*

She'll draft a new tool (name, description, parameter schema, code) using the built-in `create_skill`. It's saved as **pending**. You approve it with `/approve <name>` in the chat, or discard with `/reject <name>`. Once approved, it's live — no restart needed.

Dynamic skill code runs in a sandbox: AST-validated, restricted imports (`json`, `math`, `datetime`, `re`, `urllib.parse`, `httpx`), no async, 10-second timeout, no filesystem or arbitrary-module access. See `tools/__init__.py` for the sandbox implementation.

### Customize personality

- `config/soul.md` — who Eva is. Rewrite freely to fit whatever assistant you want.
- `config/system_prompt.txt` — the full prompt template (soul + user profile + instructions).

No code changes needed. Restart to apply.

### Connect external apps

With `COMPOSIO_API_KEY` set:

```
/connect googlecalendar
/connect gmail
/connect slack
/connect github
```

Each opens an OAuth link. Once authorized, the app's tools become available to Eva automatically.

## Architecture

```
eva/
├── main.py              # Telegram handlers, message routing, scheduled jobs
├── agent.py             # LLM interaction + tool-call loop (OpenRouter)
├── memory.py            # Supabase helpers (messages, reminders, profiles, skills)
├── composio_bridge.py   # Composio tool loading + execution
├── tools/
│   ├── __init__.py      # Auto-discovery + dynamic-skill sandbox
│   ├── create_skill.py  # Propose a new tool (pending → /approve)
│   ├── *.py             # Built-in tools
├── config/
│   ├── soul.md          # Assistant persona
│   └── system_prompt.txt
├── supabase/
│   └── schema.sql       # Tables + RLS policies
├── requirements.txt
├── railway.toml
└── .env.example
```

### How a message flows

1. User sends a message (text / voice / image / PDF) on Telegram.
2. `main.py` handler authorizes the user (`ALLOWED_USER_IDS`), enforces rate limits, decodes media if needed.
3. `agent.py` loads the last 20 messages + user profile from Supabase, assembles the system prompt, and calls OpenRouter with the active tool list (built-ins + connected Composio apps + approved dynamic skills).
4. If the LLM wants to call a tool, `agent.py` executes it and feeds the result back. Up to 5 tool rounds per message.
5. Final reply is sent to Telegram and saved to Supabase.
6. A 60-second background job checks for due reminders. A daily job (if `OWNER_USER_ID` is set) sends the morning briefing.

## Security notes

- **Always set `ALLOWED_USER_IDS`.** Without it, anyone who discovers your bot can spend your API budget.
- Use the Supabase **service_role** key. The schema applies RLS that denies `anon` entirely — the bot is the only thing that should talk to the DB.
- Dynamic skills are sandboxed but sandboxes are hard. Review every `/approve`d skill like you'd review a PR from a stranger.
- Voice transcription, TTS, and vision all send raw content to external providers (Groq, OpenRouter). Read their data policies before sending anything sensitive.
- `.env` is gitignored. Double-check before `git push` that you didn't commit any secrets.

## Roadmap / ideas

This is a personal side project, open-sourced in case it's useful to others. It is not actively supported as a product. PRs welcome for:

- Better prompt/memory management (summarization, vector search over long history)
- More Telegram surfaces (inline, buttons, edit flows)
- Multi-user mode with proper tenancy
- More rigorous skill sandboxing (subprocess isolation)

## License

[MIT](LICENSE) — do whatever you want with it.
