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
    project_id = str((cfg.get("backend_options") or {}).get("project_id") or "")

    # Completions stream A: the by_completion_date log. This reliably carries
    # one-off (non-recurring) completions, but RECURRING tasks that reset for
    # their next occurrence often drop out of this endpoint's window — so on
    # its own it silently starves the ledger of everyday habit rewards.
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

    now = datetime.now(timezone.utc)
    hold = []  # seed with the completion's own timestamp when we have one
    for item in payload.get("items", []):
        if project_id and str(item.get("project_id")) != project_id:
            continue  # ownership: only this scope's project belongs to this ledger
        hold.append({
            "id": str(item.get("id")),
            "title": (item.get("content") or "task")[:80],
            "completed_at": item.get("completed_at") or "",
            # Todoist's API integer is INVERTED vs the app's P1-P4 labels:
            # 4 = urgent (app P1). Passed through as the raw integer.
            "priority": str(item.get("priority") or 1),
            "category": str(cfg["backend_options"].get("category") or ""),
            "source": SOURCE,
        })

    # Completions stream B: the live tasks' completed_count. The engine dedupes
    # at day granularity (source:id:YYYY-MM-DD), so emitting ONE record per day
    # per task is enough — the count of days an occurrence was completed today
    # is what matters, and recurring tasks reset immediately after a check,
    # so completed_count>0 today maps to one today-winning occurrence.
    if project_id:
        tasks_url = f"{API}/tasks?project_id={project_id}&limit=200"
        tasks = (_get(tasks_url, token) or {}).get("results", [])
        for t in tasks:
            completed_count = t.get("completed_count") or 0
            if completed_count <= 0:
                continue
            stamp = t.get("completed_at") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
            # Give today's occurrence a concrete date; the engine's day-keying
            # collapses multiple occurrences of the same task+day into one award.
            try:
                dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                dt = now
            # Only pay for occurrences that land inside the lookback window.
            if dt < floor:
                continue
            hold.append({
                "id": str(t.get("id")),
                "title": (t.get("content") or "task")[:80],
                "completed_at": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "priority": str(t.get("priority") or 1),
                "category": str(cfg["backend_options"].get("category") or ""),
                "source": SOURCE,
            })

    # De-duplicate by source:id:day so a task caught in both streams and by
    # multiple occurrences on one day pays exactly once per day.
    out, seen_keys = [], set()
    for rec in hold:
        stamp = str(rec["completed_at"])[:10]
        key = f"{SOURCE}:{rec['id']}:{stamp}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(rec)
    return out
