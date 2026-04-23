"""One-shot export: Eva's Supabase persona + notes into Hermes-compatible files.

Read-only against Supabase — produces a local output directory the user uploads
into their Hermes Railway deployment's ~/.hermes/.

Run via Railway (recommended — matches Eva's Python version + env vars):

    railway run --service eva python scripts/migrate_to_hermes.py \\
        --user-id "$OWNER_USER_ID" --out ./hermes-migration

Or in a local venv with Python 3.10+ and `pip install -r requirements.txt`:

    python scripts/migrate_to_hermes.py --user-id "$OWNER_USER_ID"

Outputs under ./hermes-migration/ (override with --out):

    SOUL.md         — copy of config/soul.md
    USER.md         — user_profiles.profile
    MEMORY.md       — user_memory.body (placeholder when empty)
    PREFERENCES.md  — user_preferences rendered with Eva's sentiment markers
    skills/*.md     — approved dynamic_skills, one per file
"""

import argparse
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional — env vars may already be set

import memory  # noqa: E402  (path fix above)


SENTIMENT_MARKER = {"positive": "+", "negative": "-", "neutral": "·"}

SKILL_SANDBOX_NOTE = (
    "> Exported from Eva's sandboxed dynamic-skill runner.\n"
    "> Allowed imports: json, math, datetime, re, urllib.parse, httpx.\n"
    "> Synchronous only (no async/await). 10-second timeout. Must return a string.\n"
    "> Hermes must decide how to run this — replicate the sandbox, run under a broader\n"
    "> interpreter, or reject imports outside the allowed set.\n"
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(f"wrote {path.relative_to(path.parent.parent)} ({len(body)} chars)")


def export_soul(out_dir: Path) -> None:
    src = REPO_ROOT / "config" / "soul.md"
    if not src.exists():
        print(f"SKIP SOUL.md: {src} not found")
        return
    _write(out_dir / "SOUL.md", src.read_text(encoding="utf-8"))


def export_user_profile(out_dir: Path, user_id: int) -> None:
    profile = memory.get_user_profile(user_id)
    if not profile:
        print("SKIP USER.md: no user_profiles row for this user")
        return
    body = profile.strip()
    if not body.lstrip().startswith("#"):
        body = "# About the user\n\n" + body
    _write(out_dir / "USER.md", body + "\n")


def export_memory(out_dir: Path, user_id: int) -> None:
    body = (memory.get_user_memory(user_id) or "").strip()
    if not body:
        body = "_(empty — Eva will populate this herself)_"
    _write(out_dir / "MEMORY.md", body + "\n")


def export_preferences(out_dir: Path, user_id: int) -> None:
    prefs = memory.get_preferences(user_id, limit=500)
    if not prefs:
        print("SKIP PREFERENCES.md: no preferences stored")
        return
    lines = ["# Learned preferences", ""]
    for p in prefs:
        topic = p.get("topic") or "?"
        sentiment = p.get("sentiment") or "neutral"
        preference = (p.get("preference") or "").strip()
        marker = SENTIMENT_MARKER.get(sentiment, "·")
        lines.append(f"{marker} [{topic}] {preference}")
    _write(out_dir / "PREFERENCES.md", "\n".join(lines) + "\n")


def export_skills(out_dir: Path) -> None:
    # load_dynamic_skills() already filters to enabled + status='approved' and returns
    # the full rows (including code + parameters), which is what we need here.
    try:
        skills = memory.load_dynamic_skills() or []
    except Exception as e:
        print(f"WARN: could not load dynamic_skills: {type(e).__name__}: {e}")
        return
    if not skills:
        print("SKIP skills/: no approved dynamic skills")
        return

    skills_dir = out_dir / "skills"
    for s in skills:
        name = s.get("name")
        if not name:
            continue
        desc = (s.get("description") or "").strip().replace("\n", " ")
        params = s.get("parameters") or {}
        try:
            params_json = json.dumps(params, ensure_ascii=False, indent=2)
        except Exception:
            params_json = str(params)
        code = (s.get("code") or "").rstrip()

        body = (
            "---\n"
            f"name: {name}\n"
            f"description: {desc}\n"
            "---\n\n"
            f"{SKILL_SANDBOX_NOTE}\n"
            "## Parameters\n\n"
            f"```json\n{params_json}\n```\n\n"
            "## Code\n\n"
            f"```python\n{code}\n```\n"
        )
        _write(skills_dir / f"{name}.md", body)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Eva's persona + user notes into Hermes-compatible files."
    )
    parser.add_argument(
        "--user-id", type=int, default=None,
        help="Telegram user_id to export. Defaults to $OWNER_USER_ID from env.",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("./hermes-migration"),
        help="Output directory (default: ./hermes-migration).",
    )
    args = parser.parse_args()

    user_id = args.user_id or int(os.environ.get("OWNER_USER_ID") or 0)
    if not user_id:
        parser.error("missing --user-id (or set OWNER_USER_ID in env)")

    for key in ("SUPABASE_URL", "SUPABASE_KEY"):
        if not os.environ.get(key):
            parser.error(f"missing env var: {key} (needed to read from Supabase)")

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Exporting Eva → Hermes for user_id={user_id}")
    print(f"Output directory: {out_dir}\n")

    export_soul(out_dir)
    export_user_profile(out_dir, user_id)
    export_memory(out_dir, user_id)
    export_preferences(out_dir, user_id)
    export_skills(out_dir)

    print("\nDone. Next steps:")
    print(f"  1. Review the files under {out_dir}")
    print("  2. Upload them into your Hermes Railway deployment's ~/.hermes/ dir")
    print("     (SOUL.md / USER.md / MEMORY.md / PREFERENCES.md and skills/*.md).")
    print("  3. Create a Composio MCP server at platform.composio.dev and add its")
    print("     URL + api key to Hermes's mcp config (typically ~/.hermes/mcp.json).")
    print("  4. Smoke-test via Telegram — see the plan's Verification section.")


if __name__ == "__main__":
    main()
