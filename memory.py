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
