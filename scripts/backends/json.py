"""Generic JSON backend — the extension point.

Lets anyone wire a task source the shipped backends do not cover (a habit
tracker, a spreadsheet export, a bespoke script) without writing Python:

    {"id": "...", "title": "...", "completed_at": "...", "priority": "1".."4"}

Reads a JSON array or newline-delimited JSON objects. Only `id` and
`completed_at` are required; missing titles and priorities fall back safely.
"""
from __future__ import annotations

import json
from pathlib import Path

SOURCE = "json"


def _records(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return []
    if raw.lstrip().startswith("["):
        return json.loads(raw)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def list_completions(since_iso: str, cfg: dict) -> list[dict]:
    path = Path(cfg["backend_options"]["path"]).expanduser()
    if not path.exists():
        return []
    out = []
    for rec in _records(path):
        if not rec.get("id") or not rec.get("completed_at"):
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
