"""Generic JSON backend — the extension point.

Lets anyone wire a task source the shipped backends do not cover (a habit
tracker, a spreadsheet export, a bespoke script) without writing Python:

    {"id": "...", "title": "...", "completed_at": "...", "priority": "1".."4"}

Reads a JSON array or newline-delimited JSON objects. Only `id` and
`completed_at` are required; missing titles and priorities fall back safely.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = "json"


def validate_file(path: Path) -> str | None:
    """Return a setup-friendly error, or None for a valid JSON/NDJSON file."""
    if not path.is_file():
        return "path must be a regular file"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return str(exc)
    if not raw:
        return None
    if raw.lstrip().startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"invalid JSON: {exc.msg} at line {exc.lineno}"
        return None if isinstance(value, list) else "top-level JSON must be an array"
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            return f"invalid NDJSON at line {number}: {exc.msg}"
        if not isinstance(value, dict):
            return f"NDJSON line {number} must be an object"
    return None


def _records(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return []
    if raw.lstrip().startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []
    records = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def list_completions(since_iso: str, cfg: dict) -> list[dict]:
    path = Path(cfg["backend_options"]["path"]).expanduser()
    if not path.is_file():
        return []
    cutoff = None
    if since_iso:
        try:
            cutoff = datetime.fromisoformat(since_iso.replace("Z", "+00:00")) - timedelta(days=1)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            cutoff = None
    out = []
    for rec in _records(path):
        if not isinstance(rec, dict):
            continue
        if not rec.get("id") or not rec.get("completed_at"):
            continue
        if cutoff is not None:
            try:
                completed = datetime.fromisoformat(
                    str(rec["completed_at"]).replace("Z", "+00:00"))
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=timezone.utc)
                if completed < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
        out.append({
            "id": str(rec["id"]),
            "title": str(rec.get("title") or "task")[:80],
            "completed_at": str(rec["completed_at"]),
            "priority": str(rec.get("priority") or 1),
            "category": str(rec.get("category") or ""),
            "source": SOURCE,
        })
    return out
