"""Todoist backend.

Reads completed tasks from the REST v1 API. Works on the free plan, but the
free tier TRUNCATES completion history (a 7-day, 30-day and 90-day window can
all return the same set, and a 365-day window returns nothing). Poll at least
daily or completions age out and the rewards are gone for good.

Requires TODOIST_API_TOKEN in the environment. Never written to config.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = "todoist"
API = "https://api.todoist.com/api/v1"
MAX_LOOKBACK_DAYS = 7


def env_candidates() -> list[Path]:
    """Every .env worth searching, most-specific first.

    A skill can be installed into any profile, and each profile has its own
    .env. Hardcoding one path is what makes a skill work for its author and
    fail for everyone else.
    """
    out: list[Path] = []
    home = os.environ.get("HERMES_HOME")
    if home:
        out.append(Path(home) / ".env")
    out.append(Path.home() / ".hermes" / ".env")
    profiles = Path.home() / ".hermes" / "profiles"
    if profiles.is_dir():
        out.extend(sorted(profiles.glob("*/.env")))
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def _read_env_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            v = v.strip().strip('"').strip("'")
            if v:
                return v
    return None


def find_token() -> tuple[str | None, Path | None]:
    """Return (token, source_file). source_file is None when it came from the
    environment, so callers can report *where* it was found without ever
    printing the value itself."""
    tok = os.environ.get("TODOIST_API_TOKEN")
    if tok:
        return tok, None
    for path in env_candidates():
        value = _read_env_value(path, "TODOIST_API_TOKEN")
        if value:
            return value, path
    return None, None


def _token() -> str:
    """The API token, or a RuntimeError that says exactly where we looked."""
    tok, _src = find_token()
    if tok:
        return tok
    looked = "\n".join(f"  - {p}" for p in env_candidates())
    raise RuntimeError(
        "TODOIST_API_TOKEN not found. Set it in the environment, or add it to "
        "one of these .env files:\n" + looked
    )


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def list_projects() -> list[dict]:
    """Live project list, for the setup wizard."""
    payload = _get(f"{API}/projects", _token())
    items = payload
    if isinstance(payload, dict):
        items = payload.get("results") or payload.get("items") or []
    return sorted(items, key=lambda p: str(p.get("name", "")).lower())


def list_completions(since_iso: str, cfg: dict) -> list[dict]:
    token = _token()

    # The watermark is advisory and clamped: a late sync from an offline
    # device carries an OLD completed_at, so a tight window drops it forever.
    try:
        since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except ValueError:
        since_dt = datetime.now(timezone.utc) - timedelta(days=1)
    floor = datetime.now(timezone.utc) - timedelta(days=MAX_LOOKBACK_DAYS)
    since = max(since_dt - timedelta(hours=24), floor)

    url = (
        f"{API}/tasks/completed/by_completion_date"
        f"?since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&until={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&limit=200"
    )
    payload = _get(url, token)

    project_id = str((cfg.get("backend_options") or {}).get("project_id") or "")
    out = []
    for item in payload.get("items", []):
        if project_id and str(item.get("project_id")) != project_id:
            continue  # ownership: only this scope's project belongs to this ledger
        out.append({
            "id": str(item.get("id")),
            "title": (item.get("content") or "task")[:80],
            "completed_at": item.get("completed_at") or "",
            # Todoist's API integer is INVERTED vs the app's P1-P4 labels:
            # 4 = urgent (app P1). Passed through as the raw integer.
            "priority": str(item.get("priority") or 1),
            # A project-scoped ledger maps to one category, so the per-category
            # achievement ladders follow the ledger's own scope. Overridable.
            "category": str(cfg["backend_options"].get("category") or ""),
            "source": SOURCE,
        })
    return out
