"""Supabase helpers for conversation memory and reminders."""

import os
from datetime import datetime, timezone

from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _client


def save_message(user_id: int, role: str, content: str) -> None:
    get_client().table("messages").insert({
        "user_id": user_id,
        "role": role,
        "content": content,
    }).execute()


def load_history(user_id: int, limit: int = 20) -> list[dict]:
    resp = (
        get_client()
        .table("messages")
        .select("role, content")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = resp.data[::-1]  # reverse to chronological order
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def search_messages(user_id: int, query: str, limit: int = 3) -> list[dict]:
    """Full-text search user's message history (FTS on messages.content_tsv).

    Returns a list of {role, content, created_at} dicts, chronologically ordered
    (oldest first) so callers can inject them above the recent history window.
    Returns [] on any failure — caller should treat recall as best-effort.
    """
    q = (query or "").strip()
    if len(q) < 4:
        return []
    client = get_client()
    try:
        resp = (
            client.table("messages")
            .select("role, content, created_at")
            .eq("user_id", user_id)
            .text_search(
                "content_tsv", q,
                options={"type": "websearch", "config": "simple"},
            )
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = resp.data or []
    except Exception:
        # Older supabase-py or an FTS-index-missing error — degrade gracefully.
        try:
            pattern = f"%{q[:40]}%"
            resp = (
                client.table("messages")
                .select("role, content, created_at")
                .eq("user_id", user_id)
                .ilike("content", pattern)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            rows = resp.data or []
        except Exception:
            return []
    # Return oldest first for clean prompt injection above the recent window.
    return rows[::-1]


def create_reminder(user_id: int, message: str, remind_at: str) -> dict:
    resp = (
        get_client()
        .table("reminders")
        .insert({
            "user_id": user_id,
            "message": message,
            "remind_at": remind_at,
        })
        .execute()
    )
    return resp.data[0]


def get_pending_reminders() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    resp = (
        get_client()
        .table("reminders")
        .select("*")
        .eq("sent", False)
        .lte("remind_at", now)
        .execute()
    )
    return resp.data


def mark_reminder_sent(reminder_id: int) -> None:
    get_client().table("reminders").update({"sent": True}).eq("id", reminder_id).execute()


def get_user_profile(user_id: int) -> str | None:
    resp = (
        get_client()
        .table("user_profiles")
        .select("profile")
        .eq("user_id", user_id)
        .execute()
    )
    if resp.data:
        return resp.data[0]["profile"]
    return None


def save_user_profile(user_id: int, profile: str) -> None:
    get_client().table("user_profiles").upsert({
        "user_id": user_id,
        "profile": profile,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# --- User preferences (structured feedback capture) ---

def record_preference(
    user_id: int,
    topic: str,
    preference: str,
    sentiment: str = "neutral",
    source_excerpt: str | None = None,
) -> dict:
    resp = (
        get_client()
        .table("user_preferences")
        .insert({
            "user_id": user_id,
            "topic": topic,
            "preference": preference,
            "sentiment": sentiment,
            "source_excerpt": source_excerpt,
        })
        .execute()
    )
    return resp.data[0] if resp.data else {}


def get_preferences(user_id: int, limit: int = 30) -> list[dict]:
    """Return latest-per-topic preferences (most recent first)."""
    resp = (
        get_client()
        .table("user_preferences")
        .select("topic, preference, sentiment, source_excerpt, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit * 3)  # fetch extra so dedup still yields up to `limit` topics
        .execute()
    )
    rows = resp.data or []
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        topic = r.get("topic")
        if not topic or topic in seen:
            continue
        seen.add(topic)
        deduped.append(r)
        if len(deduped) >= limit:
            break
    return deduped


def forget_preference(user_id: int, topic: str) -> int:
    """Delete all rows for this user+topic. Returns the number deleted."""
    resp = (
        get_client()
        .table("user_preferences")
        .delete()
        .eq("user_id", user_id)
        .eq("topic", topic)
        .execute()
    )
    return len(resp.data or [])


# --- Settings ---

def get_setting(key: str, default: str = "") -> str:
    resp = get_client().table("settings").select("value").eq("key", key).execute()
    return resp.data[0]["value"] if resp.data else default


def set_setting(key: str, value: str) -> None:
    get_client().table("settings").upsert({
        "key": key,
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# --- Dynamic skills ---

def load_dynamic_skills() -> list[dict]:
    resp = (
        get_client()
        .table("dynamic_skills")
        .select("*")
        .eq("enabled", True)
        .eq("status", "approved")
        .execute()
    )
    return resp.data


def create_dynamic_skill(name: str, description: str, parameters: dict, code: str, created_by: int) -> dict:
    resp = (
        get_client()
        .table("dynamic_skills")
        .insert({
            "name": name,
            "description": description,
            "parameters": parameters,
            "code": code,
            "created_by": created_by,
        })
        .execute()
    )
    return resp.data[0]


def update_dynamic_skill(name: str, **fields) -> dict:
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    resp = (
        get_client()
        .table("dynamic_skills")
        .update(fields)
        .eq("name", name)
        .execute()
    )
    return resp.data[0] if resp.data else {}


def delete_dynamic_skill(name: str) -> bool:
    resp = (
        get_client()
        .table("dynamic_skills")
        .delete()
        .eq("name", name)
        .execute()
    )
    return len(resp.data) > 0


def list_dynamic_skills() -> list[dict]:
    resp = (
        get_client()
        .table("dynamic_skills")
        .select("name, description, enabled, status, created_at")
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data


def get_dynamic_skill(name: str) -> dict | None:
    resp = (
        get_client()
        .table("dynamic_skills")
        .select("*")
        .eq("name", name)
        .execute()
    )
    return resp.data[0] if resp.data else None


def approve_dynamic_skill(name: str) -> dict | None:
    resp = (
        get_client()
        .table("dynamic_skills")
        .update({"status": "approved", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("name", name)
        .eq("status", "pending")
        .execute()
    )
    return resp.data[0] if resp.data else None
