"""Tool: web search via Tavily API."""

import os

from tavily import TavilyClient

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use this when you need up-to-date facts, "
            "news, or answers that may not be in your training data. For fresh NEWS, set "
            "topic='news' and optionally time_range='day' (last 24h) or 'week'. "
            "For general knowledge, leave both unset."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "'news' for time-sensitive current events; 'general' for everything else.",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "Restrict to pages published within this window. Useful with topic='news'.",
                },
            },
            "required": ["query"],
        },
    },
}


def run(args: dict) -> str:
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    query = args["query"]
    kwargs = {"query": query, "max_results": 5}
    topic = (args.get("topic") or "").strip().lower()
    if topic in ("general", "news"):
        kwargs["topic"] = topic
    time_range = (args.get("time_range") or "").strip().lower()
    if time_range in ("day", "week", "month", "year"):
        kwargs["time_range"] = time_range
    results = client.search(**kwargs)
    lines = []
    for r in results.get("results", []):
        published = r.get("published_date") or ""
        header = f"**{r['title']}**"
        if published:
            header += f"  _({published})_"
        lines.append(f"{header}\n{r['url']}\n{r.get('content', '')}\n")
    return "\n---\n".join(lines) if lines else "No results found."
