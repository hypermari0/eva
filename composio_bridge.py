"""Composio integration — loads external tools (Google Calendar, etc.) and handles execution."""

import logging
import os

logger = logging.getLogger(__name__)

_toolset = None
_composio_available = False

# Map of tool name -> Composio Action for execution
_action_map: dict[str, object] = {}


def _init():
    global _toolset, _composio_available
    if _toolset is not None:
        return

    api_key = os.environ.get("COMPOSIO_API_KEY")
    if not api_key:
        logger.info("COMPOSIO_API_KEY not set — Composio tools disabled")
        _composio_available = False
        return

    try:
        from composio import ComposioToolSet, App
        _toolset = ComposioToolSet(api_key=api_key)
        _composio_available = True
        logger.info("Composio initialized")
    except Exception:
        logger.exception("Failed to initialize Composio")
        _composio_available = False


def get_tools(entity_id: str) -> list[dict]:
    """Return OpenAI-compatible tool schemas for all connected Composio apps."""
    _init()
    if not _composio_available:
        return []

    try:
        from composio import App
        tools = _toolset.get_tools(
            apps=[App.GOOGLECALENDAR],
            entity_id=entity_id,
        )

        result = []
        for tool in tools:
            # Composio returns OpenAI-compatible schemas
            if isinstance(tool, dict):
                schema = tool
            else:
                # Some versions return objects with a .model_dump() method
                schema = tool.model_dump() if hasattr(tool, "model_dump") else tool.dict()

            name = schema.get("function", {}).get("name", "")
            if name:
                _action_map[name] = name  # store for execution lookup
                result.append(schema)

        logger.info(f"Loaded {len(result)} Composio tools for entity {entity_id}")
        return result
    except Exception:
        logger.exception("Failed to load Composio tools")
        return []


def is_composio_tool(tool_name: str) -> bool:
    """Check if a tool name belongs to Composio."""
    return tool_name in _action_map


def execute(tool_name: str, args: dict, entity_id: str) -> str:
    """Execute a Composio tool call."""
    _init()
    if not _composio_available:
        return "Error: Composio is not configured."

    try:
        # Remove internal _user_id before passing to Composio
        params = {k: v for k, v in args.items() if not k.startswith("_")}
        result = _toolset.execute_action(
            action=tool_name,
            params=params,
            entity_id=entity_id,
        )

        # Result can be a dict or string
        if isinstance(result, dict):
            if result.get("error"):
                return f"Error: {result['error']}"
            data = result.get("data", result)
            if isinstance(data, dict):
                # Format key fields for readability
                parts = []
                for k, v in data.items():
                    if v is not None and v != "":
                        parts.append(f"**{k}**: {v}")
                return "\n".join(parts) if parts else str(data)
            return str(data)
        return str(result)
    except Exception as e:
        logger.exception(f"Composio tool {tool_name} failed")
        return f"Error executing {tool_name}: {e}"


def initiate_connection(entity_id: str, app_name: str) -> str | None:
    """Start OAuth flow for a user. Returns the redirect URL or None on failure."""
    _init()
    if not _composio_available:
        return None

    try:
        entity = _toolset.get_entity(id=entity_id)
        connection = entity.initiate_connection(app_name=app_name)
        return connection.redirectUrl
    except Exception:
        logger.exception(f"Failed to initiate {app_name} connection for {entity_id}")
        return None


def check_connection(entity_id: str, app_name: str) -> bool:
    """Check if a user has an active connection for an app."""
    _init()
    if not _composio_available:
        return False

    try:
        entity = _toolset.get_entity(id=entity_id)
        entity.get_connection(app=app_name)
        return True
    except Exception:
        return False
