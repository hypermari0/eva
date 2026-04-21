"""Composio integration — loads external tools dynamically and handles execution.

**Lazy bucketing**: by default we expose zero Composio action schemas to the LLM.
Instead the LLM sees a small `<external_apps>` block in the system prompt listing
which apps are connected, plus a `load_app_tools(app_name)` local tool. Calling
it queues that app's schemas for the NEXT tool round. This keeps prompt size
bounded regardless of how many apps are connected, which is the single biggest
driver of per-turn token cost and model confusion.
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# TTL on the per-entity "what apps are connected" cache — Composio network call
# is slow (~200-800ms) and connection set changes only on /connect.
CONNECTIONS_TTL = 300  # seconds

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

# Map of tool name -> Composio Action for execution. Populated on demand as
# app schemas are loaded (used by is_composio_tool()).
_action_map: dict[str, object] = {}

# Per-process cache: app_name (lowercase) -> list[tool schema dict]. Schemas
# don't change within a process lifetime so this is safe.
_app_schema_cache: dict[str, list[dict]] = {}

# Per-entity cache of connected app names, with timestamp for TTL.
_connected_apps_cache: dict[str, tuple[float, set[str]]] = {}

# Per-entity set of apps the LLM asked to load this turn — drained at the top
# of each tool round in agent.chat() and added to the active tool list.
_pending_app_loads: dict[str, set[str]] = {}

# Short human-readable descriptions for the system-prompt <external_apps>
# block. Unknown apps fall back to the raw name.
_APP_DESCRIPTIONS = {
    "gmail": "Gmail — read, search, send, draft, label emails",
    "googlecalendar": "Google Calendar — list, create, update, delete events",
    "slack": "Slack — channels, messages, DMs",
    "github": "GitHub — repos, issues, PRs, code search",
    "notion": "Notion — pages, databases, search",
    "googledrive": "Google Drive — files, folders, share",
    "googledocs": "Google Docs — create, read, update documents",
    "linkedin": "LinkedIn — profile, posts, search",
}


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


def get_connected_apps(entity_id: str) -> set[str]:
    """Return the set of app names this entity has active connections for (cached, TTL)."""
    _init()
    if not _composio_available:
        return set()
    now = time.time()
    entry = _connected_apps_cache.get(entity_id)
    if entry and (now - entry[0]) < CONNECTIONS_TTL:
        return entry[1]
    try:
        entity = _toolset.get_entity(id=entity_id)
        try:
            connections = entity.get_connections() or []
        except Exception:
            logger.info(f"No connections found for entity {entity_id}")
            _connected_apps_cache[entity_id] = (now, set())
            return set()
        apps = set()
        for conn in connections:
            name = conn.appUniqueId or conn.appName
            if name:
                apps.add(name.lower())
        _connected_apps_cache[entity_id] = (now, apps)
        logger.info(f"Entity {entity_id} connected apps: {sorted(apps)}")
        return apps
    except Exception:
        logger.exception(f"Failed to get connections for {entity_id}")
        return set()


def invalidate_connections(entity_id: str) -> None:
    """Drop the connections cache for this entity — call after /connect succeeds."""
    _connected_apps_cache.pop(entity_id, None)
    _pending_app_loads.pop(entity_id, None)


def get_app_schemas(app_name: str) -> list[dict]:
    """Return OpenAI-compatible tool schemas for a single app (process-lifetime cached)."""
    _init()
    if not _composio_available:
        return []
    key = app_name.lower()
    if key in _app_schema_cache:
        return _app_schema_cache[key]
    try:
        from composio import App
        app = App(key)
        action_models = _toolset.get_action_schemas(
            apps=[app],
            check_connected_accounts=False,
        )
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
        _app_schema_cache[key] = result
        logger.info(f"Cached {len(result)} actions for '{key}'")
        return result
    except Exception:
        logger.exception(f"Failed to load schemas for app '{key}'")
        _app_schema_cache[key] = []
        return []


def get_tools_for_apps(entity_id: str, apps) -> list[dict]:
    """Return unioned schemas for the given apps, filtered to those actually connected."""
    requested = {a.lower() for a in apps}
    connected = get_connected_apps(entity_id)
    usable = requested & connected
    missing = requested - connected
    if missing:
        logger.info(f"Entity {entity_id} requested un-connected apps (skipping): {sorted(missing)}")
    result = []
    for app in sorted(usable):
        result.extend(get_app_schemas(app))
    return result


def mark_pending_app(entity_id: str, app_name: str) -> None:
    """Queue an app to be added to the active tool list on the next tool round."""
    _pending_app_loads.setdefault(entity_id, set()).add(app_name.lower())


def drain_pending_apps(entity_id: str) -> set[str]:
    """Return and clear the set of apps queued via mark_pending_app for this entity."""
    return _pending_app_loads.pop(entity_id, set())


def describe_connected_apps(entity_id: str) -> str:
    """Short human-readable summary for injection into the system prompt."""
    apps = get_connected_apps(entity_id)
    if not apps:
        return "(no external apps connected)"
    lines = []
    for app in sorted(apps):
        desc = _APP_DESCRIPTIONS.get(app, app)
        lines.append(f"- {app}: {desc}")
    return "\n".join(lines)


def get_tools(entity_id: str) -> list[dict]:
    """Back-compat entry point. Returns an empty list — actual Composio schemas
    are loaded on demand via load_app_tools. Kept so older callers don't break."""
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
        # A fresh connection is about to be authorized; drop the cached app set
        # so the next turn sees it without waiting for TTL expiry.
        invalidate_connections(entity_id)
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
