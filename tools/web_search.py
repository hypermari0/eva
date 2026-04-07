"""Tool: web search via Tavily API."""

import os

from tavily import TavilyClient

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information. Use this when you need up-to-date facts, news, or answers that may not be in your training data.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
            },
            "required": ["query"],
        },
    },
}


def run(args: dict) -> str:
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    results = client.search(query=args["query"], max_results=5)
    lines = []
    for r in results.get("results", []):
        lines.append(f"**{r['title']}**\n{r['url']}\n{r.get('content', '')}\n")
    return "\n---\n".join(lines) if lines else "No results found."
