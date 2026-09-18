"""Backends for the task-rewards engine.

A backend exposes exactly one function:

    list_completions(since_iso: str, cfg: dict) -> list[dict]

Each returned record is provider-neutral:

    {"id": str, "title": str, "completed_at": ISO8601, "priority": "1".."4",
     "category": str, "source": str}

`category` is optional; return "" and the engine files it under "inbox".
`since_iso` is an advisory watermark; returning extra history is safe because
the engine dedupes, and returning too little silently loses rewards. When in
doubt, return more.
"""
