"""Markdown checklist backend.

Parses conventions users already have rather than demanding a new one:

  * Obsidian Tasks emoji format
        - [x] #task Ship the skill ✅ 2023-04-17
        - [ ] #task Write docs 🔺
        (priorities 🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest)
  * todo.txt
        x 2025-01-09 (A) 2025-01-05 Submit proposal +work
        (priority (A) highest … (Z) lowest)
  * Plain markdown checkboxes
        - [x] Buy milk
        (no date recorded — stamped at detection time; documented in the skill)

Task identity is a hash of the normalised task text, so moving a task between
files keeps its identity. Editing a task's text starts a new task. Two
identical texts get #2, #3 suffixes.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "markdown"

OBSIDIAN_DONE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
MD_CHECKBOX = re.compile(r"^\s*[-*+]\s*\[[xX]\]\s+(?P<body>.*)$")
TODOTXT = re.compile(r"^x\s+(?P<body>.*)$")
TODO_DONE_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
TODO_PRIO = re.compile(r"\(([A-Z])\)")

OBSIDIAN_PRIO = {
    "🔺": "4", "⏫": "3", "🔼": "2", "🔽": "1", "⏬": "1",
}

# Strip metadata so the identity hash survives unrelated edits.
STRIP = re.compile(
    r"(✅\s*\d{4}-\d{2}-\d{2}|➕\s*\d{4}-\d{2}-\d{2}|📅\s*\d{4}-\d{2}-\d{2}"
    r"|⏳\s*\d{4}-\d{2}-\d{2}|🛫\s*\d{4}-\d{2}-\d{2}|🔁[^\s]*"
    r"|#\w+|\^[\w-]+)"
)


def _norm(text: str) -> str:
    t = STRIP.sub(" ", text)
    for sym in OBSIDIAN_PRIO:
        t = t.replace(sym, " ")
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def _task_id(text: str) -> str:
    return hashlib.sha256(_norm(text).encode()).hexdigest()[:12]


def _iter_files(path: Path):
    if path.is_dir():
        yield from sorted(path.rglob("*.md"))
    elif path.is_file():
        yield path


def _parse_obsidian(body: str) -> tuple[str, str]:
    """-> (completed_at_iso_or_empty, priority)"""
    prio = "1"
    for sym, value in OBSIDIAN_PRIO.items():
        if sym in body:
            prio = value
            break
    m = OBSIDIAN_DONE.search(body)
    return (f"{m.group(1)}T12:00:00+00:00" if m else "", prio)


def _parse_todotxt(body: str) -> tuple[str, str]:
    prio = "1"
    mp = TODO_PRIO.search(body)
    if mp:
        letter = mp.group(1)
        prio = {"A": "4", "B": "3", "C": "2"}.get(letter, "1")
    md = TODO_DONE_DATE.search(body)
    return (f"{md.group(1)}T12:00:00+00:00" if md else "", prio)


def _clean_title(body: str) -> str:
    """Strip metadata so the displayed title is the task, not its syntax."""
    t = STRIP.sub(" ", body)          # must run BEFORE date removal
    t = TODO_PRIO.sub(" ", t)
    for sym in OBSIDIAN_PRIO:
        t = t.replace(sym, " ")
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:80]


# Tags that mark a task rather than classify it — never a category.
MARKER_TAGS = {"task", "todo", "done"}
TAG_RE = re.compile(r"#([A-Za-z0-9_\-/]+)")


def _category(body: str, path, use_folder: bool = True) -> str:
    """Classify a completion from its first real tag, else its folder.

    The folder is only meaningful when the configured path is a DIRECTORY —
    scanning `~/Vault/Gym/*.md` makes "gym" a real category, but pointing at a
    single file makes its parent folder incidental, so that falls through to
    the engine's DEFAULT_CATEGORY instead of inventing junk categories.

    Falls back to "" for the same reason — an uncategorised task must never
    crash the category ladders.
    """
    for m in TAG_RE.finditer(body):
        tag = m.group(1).lower()
        if tag not in MARKER_TAGS:
            return tag
    if not use_folder:
        return ""
    try:
        parent = path.parent.name
    except AttributeError:
        parent = ""
    return parent.lower() if parent and parent != "." else ""


def list_completions(since_iso: str, cfg: dict) -> list[dict]:
    root = Path(cfg["backend_options"]["path"]).expanduser()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        since_dt = datetime.fromisoformat(str(since_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        since_dt = None
    out: list[dict] = []
    occurrences: dict[str, int] = {}

    for f in _iter_files(root):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            body = None
            done_at = ""
            prio = "1"

            m = MD_CHECKBOX.match(line)
            if m:
                body = m.group("body")
                done_at, prio = _parse_obsidian(body)
            else:
                m = TODOTXT.match(line)
                if m:
                    body = m.group("body")
                    done_at, prio = _parse_todotxt(body)
            if body is None:
                continue

            if not done_at:
                # No machine-readable date in the file. Stamp at detection
                # time; the engine's day-granular key keeps this idempotent.
                done_at = now_iso

            base = _task_id(body)
            n = occurrences.get(base, 0) + 1
            occurrences[base] = n
            tid = base if n == 1 else f"{base}#{n}"

            # Watermark filter, at DAY granularity. File-recorded dates carry
            # no time, so comparing instants drops anything completed earlier
            # the same day the watermark was set.
            if since_dt is not None:
                try:
                    done_dt = datetime.fromisoformat(done_at.replace("Z", "+00:00"))
                except ValueError:
                    done_dt = datetime.now(timezone.utc)
                if done_dt.date() < since_dt.date():
                    continue

            out.append({
                "id": tid,
                "title": _clean_title(body),
                "completed_at": done_at,
                "priority": prio,
                "category": _category(body, f, root.is_dir()),
                "source": SOURCE,
            })
    return out
