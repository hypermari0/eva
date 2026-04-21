"""Composio integration — loads external tools dynamically and handles execution."""

import json
import logging
import os

logger = logging.getLogger(__name__)

# Cap on a single tool result; prevents 20k+ char Gmail dumps from busting the
# model context (and drowning the LLM in noise it can't use).
MAX_RESULT_CHARS = 8000

# Fields we strip from Gmail/Calendar responses because they're bulky and the
# LLM doesn't need them to summarize. Keeping subject/from/snippet/date/to for
# email, and summary/start/end/description/location/attendees for events.
_GMAIL_HEAVY = frozenset({
    "payload", "body", "raw", "internalDate", "sizeEstimate", "historyId",
    "labelIds",
})
_CALENDAR_HEAVY = frozenset({
    "htmlLink", "etag", "iCalUID", "sequence", "reminders", "creator",
    "organizer", "eventType", "kind", "conferenceData", "hangoutLink",
    "created", "updated",
})


def _strip_heavy(obj, heavy):
    if isinstance(obj, dict):
        return {k: _strip_heavy(v, heavy) for k, v in obj.items() if k not in heavy}
    if isinstance(obj, list):
        return [_strip_heavy(x, heavy) for x in obj]
    return obj


def _unwrap(payload):
    """Peel Composio's envelope keys so the LLM sees actual content, not wrappers."""
    # Some Composio responses nest real data under response_data; unwrap if
    # that's the only key or clearly the payload root.
    while isinstance(payload, dict) and len(payload) == 1 and "response_data" in payload:
        payload = payload["response_data"]
    return payload


def _serialize_result(tool_name: str, data) -> str:
    heavy = frozenset()
    if tool_name.startswith("GMAIL_"):
        heavy = _GMAIL_HEAVY
    elif tool_name.startswith("GOOGLECALENDAR_"):
        heavy = _CALENDAR_HEAVY
    trimmed = _strip_heavy(data, heavy) if heavy else data
    try:
        text = json.dumps(trimmed, ensure_ascii=False, indent=2, default=str)
    except Exception:
        text = str(trimmed)
    if len(text) > MAX_RESULT_CHARS:
        return (
            text[:MAX_RESULT_CHARS]
            + f"\n... (truncated; full result was {len(text)} chars — "
            "call the tool again with a narrower query if you need more)"
        )
    return text

_toolset = None
_composio_available = False
_init_attempted = False

# Map of tool name -> Composio Action for execution
_action_map: dict[str, object] = {}


def _init():
    global _toolset, _composio_available, _init_attempted
    if _init_attempted:
        return
    _init_attempted = True

    api_key = os.environ.get("COMPOSIO_API_KEY")
    if not api_key:
        logger.warning("COMPOSIO_API_KEY not set — Composio tools disabled")
        _composio_available = False
        return

    try:
        from composio import ComposioToolSet
        _toolset = ComposioToolSet(api_key=api_key)
        _composio_available = True
        logger.info("Composio initialized successfully")
    except Exception:
        logger.exception("Failed to initialize Composio")
        _composio_available = False


def get_tools(entity_id: str) -> list[dict]:
    """Return OpenAI-compatible tool schemas for all connected apps for this entity."""
    _init()
    if not _composio_available:
        return []

    try:
        # Get all active connections for this entity, then load their tools
        entity = _toolset.get_entity(id=entity_id)
        try:
            connections = entity.get_connections()
        except Exception:
            logger.info(f"No connections found for entity {entity_id}")
            return []

        if not connections:
            return []

        # Collect unique app names from active connections
        connected_apps = set()
        for conn in connections:
            app_name = conn.appUniqueId or conn.appName
            if app_name:
                connected_apps.add(app_name)

        if not connected_apps:
            return []

        logger.info(f"Entity {entity_id} has connections to: {connected_apps}")

        # Load action schemas for each connected app individually
        # so one broken connection doesn't block the others
        from composio import App
        action_models = []
        for app_name in connected_apps:
            try:
                app = App(app_name)
                schemas = _toolset.get_action_schemas(
                    apps=[app],
                    check_connected_accounts=False,
                )
                action_models.extend(schemas)
                logger.info(f"Loaded {len(schemas)} actions for {app_name}")
            except Exception:
                logger.warning(f"Failed to load actions for {app_name}, skipping", exc_info=True)

        if not action_models:
            logger.info(f"No action schemas loaded for entity {entity_id}")
            return []

        result = []
        for action in action_models:
            name = action.name
            if not name:
                continue

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

        # Truncate raw result in logs too so Railway logs stay sane
        raw_preview = str(result)
        if len(raw_preview) > 2000:
            raw_preview = raw_preview[:2000] + f"... (+{len(str(result)) - 2000} chars)"
        logger.info(f"Composio {tool_name} raw result: {raw_preview}")

        if isinstance(result, dict):
            if result.get("error"):
                return f"Error: {result['error']}"
            if result.get("successfull") is False or result.get("successful") is False:
                err = result.get("error") or result.get("data") or "Unknown error"
                return f"Error: {tool_name} failed — {err}"
            payload = result.get("data", result)
        else:
            payload = result

        if isinstance(payload, dict) and payload.get("error"):
            return f"Error: {payload['error']}"

        payload = _unwrap(payload)
        return _serialize_result(tool_name, payload)
    except Exception as e:
        logger.exception(f"Composio tool {tool_name} failed")
        return f"Error executing {tool_name}: {e}"


def initiate_connection(entity_id: str, app_name: str) -> str | None:
    """Start OAuth flow for a user. Returns the redirect URL or None on failure."""
    _init()
    if not _composio_available:
        logger.warning(f"initiate_connection called but Composio is not available")
        return None

    try:
        logger.info(f"Initiating {app_name} connection for entity {entity_id}")
        entity = _toolset.get_entity(id=entity_id)
        connection = entity.initiate_connection(app_name=app_name)
        url = connection.redirectUrl
        logger.info(f"Got redirect URL for {app_name}: {url[:80] if url else 'None'}...")
        return url
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
        logger.info(f"Entity {entity_id} has active {app_name} connection")
        return True
    except Exception as e:
        logger.info(f"No active {app_name} connection for entity {entity_id}: {e}")
        return False
