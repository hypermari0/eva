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
        from composio import ComposioToolSet
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
        logger.info(f"Loading Composio tools for entity {entity_id}...")
        action_models = _toolset.get_action_schemas(
            apps=[App.GOOGLECALENDAR],
        )
        logger.info(f"Got {len(action_models)} action schemas from Composio")

        result = []
        for action in action_models:
            name = action.name
            if not name:
                continue

            # Convert ActionModel to OpenAI-compatible function schema
            params = action.parameters
            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": action.description or "",
                    "parameters": {
                        "type": params.type if params else "object",
                        "properties": params.properties if params else {},
                    },
                },
            }
            if params and params.required:
                schema["function"]["parameters"]["required"] = params.required

            _action_map[name] = name
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
        logger.info(f"Composio executing {tool_name} for entity {entity_id} with params: {params}")

        result = _toolset.execute_action(
            action=tool_name,
            params=params,
            entity_id=str(entity_id),
        )

        logger.info(f"Composio {tool_name} raw result: {result}")

        # Result can be a dict or string
        if isinstance(result, dict):
            # Check for errors at multiple levels
            if result.get("error"):
                return f"Error: {result['error']}"
            if result.get("successfull") is False or result.get("successful") is False:
                error_msg = result.get("error", result.get("data", "Unknown error"))
                return f"Error: {tool_name} failed — {error_msg}"

            data = result.get("data", result)
            if isinstance(data, dict):
                # Check for nested error
                if data.get("error"):
                    return f"Error: {data['error']}"
                # Format key fields for readability
                parts = []
                for k, v in data.items():
                    if v is not None and v != "":
                        parts.append(f"{k}: {v}")
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
