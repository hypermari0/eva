# Eva — Personal AI Assistant (Telegram Bot)

A Telegram bot powered by Claude via OpenRouter, with persistent memory (Supabase) and an extensible tool system.

## Features

- **Conversational AI** — Claude Sonnet 4.5 via OpenRouter
- **Persistent memory** — conversation history stored per user in Supabase
- **Tool system** — auto-discovered from the `tools/` folder; add a file, get a skill
- **Reminders** — schedule reminders that get delivered via Telegram
- **Web search** — real-time web search via Tavily
- **Deploy-ready** — one-click deploy to Railway

## Quick Start

### 1. Create the Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Choose a name (e.g. "Eva") and a username (e.g. `eva_ai_assistant_bot`)
4. Copy the **bot token** — you'll need it for `TELEGRAM_BOT_TOKEN`

### 2. Set Up Supabase

1. Create a project at [supabase.com](https://supabase.com)
2. Go to **SQL Editor** and run the contents of `supabase/schema.sql`
3. Go to **Settings → API** and copy:
   - **Project URL** → `SUPABASE_URL`
   - **anon public key** → `SUPABASE_KEY`

### 3. Get API Keys

- **OpenRouter**: Sign up at [openrouter.ai](https://openrouter.ai), go to Keys, create one → `OPENROUTER_API_KEY`
- **Tavily**: Sign up at [tavily.com](https://tavily.com), get your API key → `TAVILY_API_KEY`

### 4. Run Locally

```bash
# Clone and enter the project
cd eva

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your actual keys

# Run
python main.py
```

### 5. Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and create a new project
3. Select **Deploy from GitHub repo** and pick this repo
4. Go to **Variables** and add all five env vars:
   - `TELEGRAM_BOT_TOKEN`
   - `OPENROUTER_API_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `TAVILY_API_KEY`
5. Railway will auto-detect `railway.toml` and deploy. That's it.

## Adding New Tools

The tool system auto-discovers any `.py` file in the `tools/` folder. To add a new tool:

1. Create a new file, e.g. `tools/my_tool.py`
2. Define two things:

```python
TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "What this tool does — the LLM reads this to decide when to use it.",
        "parameters": {
            "type": "object",
            "properties": {
                "arg_name": {
                    "type": "string",
                    "description": "What this argument is for.",
                },
            },
            "required": ["arg_name"],
        },
    },
}


def run(args: dict) -> str:
    # args["_user_id"] is always available (Telegram user ID)
    return f"Result for {args['arg_name']}"
```

3. Restart the bot. The tool is automatically available to the LLM.

## Project Structure

```
eva/
├── main.py                    # Bot entry point, Telegram handlers
├── agent.py                   # LLM interaction, tool call loop
├── memory.py                  # Supabase read/write helpers
├── tools/
│   ├── __init__.py            # Auto-discovery loader
│   ├── get_current_datetime.py
│   ├── web_search.py
│   └── create_reminder.py
├── config/
│   └── system_prompt.txt      # Editable system prompt
├── supabase/
│   └── schema.sql             # Database schema
├── requirements.txt
├── railway.toml
├── .env.example
└── .gitignore
```

## Configuration

Edit `config/system_prompt.txt` to change the bot's personality and behavior. No code changes needed — just edit the file and restart.

## How It Works

1. User sends a message on Telegram
2. Bot loads the last 20 messages from Supabase for that user
3. Sends conversation + system prompt + available tools to Claude via OpenRouter
4. If Claude calls a tool, the bot executes it and sends the result back (up to 5 rounds)
5. Final text response is sent to the user and saved to Supabase
6. A background job checks for due reminders every 60 seconds
