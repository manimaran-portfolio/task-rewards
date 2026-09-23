#!/usr/bin/env python3
"""task-rewards engine: award XP, levels and streaks for completed tasks.

Provider-agnostic core. It understands only a stream of completion records
``{id, title, completed_at, priority, source}`` produced by a *backend*.
Nothing in this file knows what Todoist or a markdown checklist is.

Stdlib only. State is one JSON ledger per scope, written atomically under a
cross-platform advisory lock.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_paths  # noqa: E402

VERSION = 1
DEFAULT_XP = {"4": 50, "3": 30, "2": 20, "1": 10}
FREEZE_EVERY = 7
FREEZE_CAP = 2
SEEN_CAP = 500  # soft target; never evict keys a backend can still return
MAX_LEVEL = 50  # hard cap: keeps the late-game requirement from growing unbounded
STREAK_BONUS = 10  # flat, once per day — deliberately NOT a per-task multiplier
HIGHLIGHT_CAP = 2  # max emphasised messages per day, per the throttle below
DEFAULT_CATEGORY = "inbox"  # where uncategorised tasks land; never a crash

# (key, label, predicate over counters, target for progress display, bonus XP)
BASE_ACHIEVEMENTS = (
    ("first_spark", "First Spark", lambda s: s["tasks_completed"] >= 1, 1, 50),
    ("getting_going", "Getting Going", lambda s: s["tasks_completed"] >= 25, 25, 30),
    ("century_club", "Century Club", lambda s: s["total_xp"] >= 1000, 1000, 200),
    ("week_one", "Week One", lambda s: s["streak_days"] >= 7, 7, 100),
    ("unbroken", "Unbroken", lambda s: s["streak_days"] >= 30, 30, 300),
)

# Per-category ladders, generated rather than authored — any category the user
# actually works in gets its own ladder for free.
CATEGORY_TIERS = (("apprentice", 10, 50), ("master", 20, 100), ("expert", 50, 200))


def all_achievements(state: dict) -> list[tuple]:
    """Base achievements plus auto-generated per-category ones."""
    out = list(BASE_ACHIEVEMENTS)
    for cat in sorted(state.get("categories", {})):
        pretty = cat.replace("_", " ").replace("-", " ").title()
        for tier, need, bonus in CATEGORY_TIERS:
            out.append((
                f"{cat}_{tier}",
                f"{pretty} {tier.title()}",
                (lambda s, c=cat, n=need: s.get("categories", {}).get(c, 0) >= n),
                need,
                bonus,
            ))
    return out


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------

def _lock_handle(fh):
    """Best-effort exclusive advisory lock. POSIX and Windows."""
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return lambda: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except ImportError:
        pass
    try:
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        return lambda: msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:
        return lambda: None


def atomic_write_json(path: Path, payload: dict) -> None:
    """temp file -> fsync -> atomic replace. Readers never see partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        # Corrupt ledger: preserve it, start clean, and say so loudly once.
        backup = path.with_suffix(f".corrupt.{int(datetime.now().timestamp())}.json")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        payload = dict(default)
        payload["_recovered_from"] = backup.name
        return payload


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_today(tzname: str) -> date:
    """Calendar date in the user's timezone.

    Streaks are a human-calendar concept; evaluating them in UTC breaks every
    user whose evening lands after midnight UTC.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname)).date()
    except Exception:
        return datetime.now().date()


def completion_date(rec: dict, tzname: str = "UTC") -> date:
    """Return a completion timestamp's calendar date in the configured zone."""
    stamp = str(rec.get("completed_at") or "")
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(tzname)).date()
    except Exception:
        return dt.astimezone(timezone.utc).date()


# --------------------------------------------------------------------------
# mechanics
# --------------------------------------------------------------------------

def xp_to_next(level: int) -> int:
    """XP needed to advance FROM `level`. Delta curve, not cumulative.

    Returns 0 once the cap is reached; every caller must treat 0 as
    "no further levels" rather than as a free level.
    """
    if level >= MAX_LEVEL:
        return 0
    return round(100 * (level ** 1.5))


def normalize_record(rec) -> dict | None:
    """Enforce the ingest contract.

    The engine understands exactly {id, title, completed_at, priority, source}.
    Backends may carry richer data (due dates, durations, categories); it is
    dropped here so no backend can widen the scoring surface. Returns None for
    anything malformed, so bad backend output skips rather than corrupting.
    """
    if not isinstance(rec, dict):
        return None
    rid = rec.get("id")
    stamp = rec.get("completed_at")
    if not rid or not stamp:
        return None
    try:
        datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    prio = rec.get("priority")
    if prio is not None:
        try:
            prio = int(prio)
        except (TypeError, ValueError):
            prio = None
    title = str(rec.get("title") or "").strip()[:120]
    cat = str(rec.get("category") or "").strip().lower().replace(" ", "_")
    return {
        "id": str(rid),
        "title": title or "(untitled)",
        "completed_at": str(stamp),
        "priority": prio,
        "category": cat or DEFAULT_CATEGORY,  # graceful degradation, never a KeyError
        "source": str(rec.get("source") or ""),
    }


def dedupe_key(rec: dict, tzname: str = "UTC") -> str:
    """task identity at day granularity.

    Keying on the raw timestamp would re-award when a user unchecks and
    rechecks a task (Todoist issues a fresh completed_at). Day granularity
    absorbs that, and lets a recurring task legitimately pay once per day.
    """
    day = completion_date(rec, tzname).isoformat()
    basis = f"{rec.get('source','')}:{rec.get('id','')}:{day}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def score(rec: dict, xp_table: dict) -> int:
    prio = str(rec.get("priority") or "1")
    return int(xp_table.get(prio, xp_table.get("1", 10)))


def _advance_levels(state: dict) -> list[int]:
    """Roll surplus XP into levels, stopping hard at MAX_LEVEL."""
    leveled = []
    while state["level"] < MAX_LEVEL and state["xp_into_level"] >= xp_to_next(state["level"]):
        state["xp_into_level"] -= xp_to_next(state["level"])
        state["level"] += 1
        leveled.append(state["level"])
    if state["level"] >= MAX_LEVEL:
        state["xp_into_level"] = 0  # capped; lifetime total keeps accruing
    return leveled


def apply_completion(state: dict, xp: int, today: date,
                     category: str = DEFAULT_CATEGORY) -> dict:
    """Fold one completion into the player state."""
    today_iso = today.isoformat()

    # streak first, so the daily bonus below sees the updated value
    last = state.get("last_active_date")
    late = False
    if last == today_iso:
        pass
    elif last is None:
        state["streak_days"] = state.get("streak_days", 0) + 1
        state["freeze_progress"] = state.get("freeze_progress", 0) + 1
    else:
        last_date = date.fromisoformat(last)
        gap = (today - last_date).days
        if gap < 0:
            late = True
        elif gap == 1:
            state["streak_days"] = state.get("streak_days", 0) + 1
            state["freeze_progress"] = state.get("freeze_progress", 0) + 1
        else:
            missed_days = max(0, gap - 1)
            freezes = state.get("freezes", 0)
            if missed_days <= freezes:
                state["freezes"] = freezes - missed_days
                state["streak_days"] = state.get("streak_days", 0) + 1
                state["freeze_progress"] = state.get("freeze_progress", 0) + 1
            else:
                state["freezes"] = 0
                state["streak_days"] = 1
                state["freeze_progress"] = 1
    if not late:
        if state.get("freeze_progress", 0) >= FREEZE_EVERY:
            state["freezes"] = min(FREEZE_CAP, state.get("freezes", 0) + 1)
            state["freeze_progress"] = 0
        state["last_active_date"] = today_iso
        state["longest_streak"] = max(state.get("longest_streak", 0), state["streak_days"])

    # Flat once-per-day streak bonus, paid on the first completion of the day.
    # Deliberately NOT a per-task multiplier: at x2, clearing 15 trivial
    # subtasks would bank 30 tasks' worth of XP for one day of consistency.
    bonus = 0
    if not late and state.get("streak_bonus_date") != today_iso:
        tier = 1 + (1 if state["streak_days"] >= 10 else 0) \
                 + (1 if state["streak_days"] >= 30 else 0)
        bonus = STREAK_BONUS * tier
        state["streak_bonus_date"] = today_iso
    state["_bonus"] = bonus

    gained = xp + bonus
    state["tasks_completed"] = state.get("tasks_completed", 0) + 1
    state["total_xp"] = state.get("total_xp", 0) + gained
    state["xp_into_level"] = state.get("xp_into_level", 0) + gained
    state["level"] = state.get("level", 1)

    cats = state.setdefault("categories", {})
    cats[category] = cats.get(category, 0) + 1

    state["_leveled"] = _advance_levels(state)
    return state


def effective_streak(state: dict, today: date) -> tuple[int, bool]:
    """The streak as it actually stands today, without mutating the ledger.

    `streak_days` in the ledger is only ever corrected by apply_completion, on
    the NEXT real completion — there is no cron job that walks in and zeroes
    it out. That is fine for scoring (a freeze/reset is applied correctly the
    moment it matters) but it means a stale in-ledger streak_days can lag
    reality by several silent days. Every renderer and the streak-check
    warning must go through this instead of reading state["streak_days"]
    directly, so display is never more optimistic than the truth.

    Returns (effective_streak_days, still_protected_by_a_freeze).
    """
    streak = state.get("streak_days", 0)
    last = state.get("last_active_date")
    if not last or streak == 0:
        return streak, False
    try:
        last_d = date.fromisoformat(last)
    except ValueError:
        return streak, False
    gap = (today - last_d).days
    if gap <= 1:
        return streak, False
    missed_days = gap - 1
    if state.get("freezes", 0) >= missed_days:
        return streak, True  # a freeze covers exactly this one gap, not consumed until it's real
    # Framed as a beginning, never a loss, once the caller surfaces this: punishment
    # framing is what makes people quit a streak system outright.
    return 0, False


def _progress(state: dict, key: str, target: int) -> int:
    """Current counter value for a locked achievement's progress display."""
    if key in ("first_spark", "getting_going"):
        cur = state.get("tasks_completed", 0)
    elif key == "century_club":
        cur = state.get("total_xp", 0)
    elif key in ("week_one", "unbroken"):
        cur = state.get("streak_days", 0)
    else:  # a generated category achievement, keyed "{category}_{tier}"
        cur = state.get("categories", {}).get(key.rsplit("_", 1)[0], 0)
    return min(cur, target)


def check_achievements(state: dict) -> list[tuple[str, int]]:
    """Unlock newly-earned achievements and pay their bonuses exactly once.

    Returns the new unlocks as (label, bonus) so the caller can surface them.
    """
    have = state.setdefault("achievements", [])
    newly = []
    for key, label, pred, _target, bonus in all_achievements(state):
        if key in have or not pred(state):
            continue
        have.append(key)
        if bonus:
            state["total_xp"] = state.get("total_xp", 0) + bonus
            state["xp_into_level"] = state.get("xp_into_level", 0) + bonus
        newly.append((label, bonus))
    return newly


def earned_achievements(state: dict) -> list[tuple[str, str, bool, int, int]]:
    """Read-only view for --status.

    Unlocking is NOT a side effect of rendering: an achievement must only ever
    be granted by check_achievements, or the bonus could be paid twice.
    """
    out = []
    for key, label, pred, target, _bonus in all_achievements(state):
        out.append((key, label, pred(state), _progress(state, key, target), target))
    return out


def take_highlight(state: dict, today: date) -> bool:
    """Ration emphasised messages to HIGHLIGHT_CAP per day.

    Without this every level-up, unlock and streak milestone shouts at once and
    the whole thing becomes noise the user mutes. Rationing is why the messages
    keep their impact.
    """
    iso = today.isoformat()
    if state.get("highlight_date") != iso:
        state["highlight_date"] = iso
        state["daily_highlights"] = 0
    if state.get("daily_highlights", 0) >= HIGHLIGHT_CAP:
        return False
    state["daily_highlights"] = state.get("daily_highlights", 0) + 1
    return True


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def bar(current: int, target: int, width: int = 10) -> str:
    if target <= 0:
        return ""
    filled = max(0, min(width, round(width * current / target)))
    return "▰" * filled + "▱" * (width - filled)


def status_payload(state: dict, scope: str, today: date) -> dict:
    """Everything --status (and a bot's --status --json) needs, computed once.

    Achievement unlock/progress is derived here from the live counters on
    every call — nothing here is a stored "is unlocked" flag being replayed.
    The only thing ever persisted about an achievement is that its one-time
    bonus was already paid (see check_achievements), which is a dedupe
    concern, not the source of truth for whether it's earned.
    """
    streak_days, protected = effective_streak(state, today)
    need = xp_to_next(state["level"])
    achs = [
        {"key": key, "label": label, "unlocked": unlocked, "current": cur, "target": target}
        for key, label, unlocked, cur, target in earned_achievements(state)
    ]
    return {
        "scope": scope,
        "level": state["level"],
        "max_level": need == 0,
        "xp_into_level": state.get("xp_into_level", 0),
        "xp_to_next_level": need,
        "total_xp": state.get("total_xp", 0),
        "tasks_completed": state.get("tasks_completed", 0),
        "streak_days": streak_days,
        "streak_protected_by_freeze": protected,
        "longest_streak": state.get("longest_streak", 0),
        "freezes": state.get("freezes", 0),
        "achievements_unlocked": sum(1 for a in achs if a["unlocked"]),
        "achievements_total": len(achs),
        "achievements": achs,
    }


def render_poll(payload: dict) -> str:
    """The completion ping. Empty string when there is nothing to say."""
    batch = payload["batch"]
    if not batch:
        return ""
    lines = [f"⚡ +{payload['gained']} XP", ""]
    for b in batch[:8]:
        lines.append(f"{b['title'][:38]:<38} +{b['xp']}")
    if payload["bonus_total"]:
        lines.append(f"{'🔥 daily streak bonus':<38} +{payload['bonus_total']}")
    if len(batch) > 8:
        lines.append(f"...and {len(batch) - 8} more")
    # The legs of the ladder that were rolled into levels, then the streak
    # milestone. Only a rationed subset gets an emphasised line.
    lines.append("")
    for line in payload["emphasis"]:
        lines.append(line)
    if payload["xp_to_next_level"]:
        lines.append(f"🏆 Level {payload['level']}  {bar(payload['xp_into_level'], payload['xp_to_next_level'])}  "
                     f"{payload['xp_into_level']}/{payload['xp_to_next_level']} XP")
    else:
        lines.append(f"🏆 Level {payload['level']} — MAX")
    lines.append(f"🔥 Streak {payload['streak_days']}d (best {payload['longest_streak']}d)   "
                 f"🛡️ {payload['freezes']} freeze")
    return "\n".join(lines)


def render_status(payload: dict, compact: bool = False) -> str:
    level_line = (
        f"Level {payload['level']}   {bar(payload['xp_into_level'], payload['xp_to_next_level'])}   "
        f"{payload['xp_into_level']}/{payload['xp_to_next_level']} XP"
        if payload["xp_to_next_level"] else f"Level {payload['level']} — MAX"
    )
    streak_suffix = "  (protected by a freeze)" if payload["streak_protected_by_freeze"] else ""
    head = [
        f"🏆 Rewards — {payload['scope']}",
        "",
        level_line,
        f"🔥 Streak {payload['streak_days']}d (best {payload['longest_streak']}d)   "
        f"🛡️ {payload['freezes']} freeze banked{streak_suffix}",
        f"📊 {payload['tasks_completed']} tasks · {payload['total_xp']} XP lifetime",
    ]
    if compact:
        summary = (
            f"🔥 {payload['streak_days']} day streak · "
            f"🛡️ {payload['freezes']} freeze banked · "
            f"📊 {payload['tasks_completed']} tasks · {payload['total_xp']} XP lifetime"
        )
        return "\n".join((level_line, summary))
    head += ["", f"Achievements   {payload['achievements_unlocked']} / {payload['achievements_total']}"]
    for a in payload["achievements"]:
        if a["unlocked"]:
            head.append(f"✅ {a['label']}")
        else:
            head.append(f"🔒 {a['label']}  ({a['current']}/{a['target']})")
    return "\n".join(head)


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

def load_backend(name: str):
    """Import a backend module by name from scripts/backends/."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import importlib
    return importlib.import_module(f"backends.{name}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def default_state(scope: str) -> dict:
    return {
        "version": VERSION,
        "scope": scope,
        "created_at": now_utc().isoformat(),
        "baseline_at": None,
        "level": 1,
        "xp_into_level": 0,
        "total_xp": 0,
        "tasks_completed": 0,
        "streak_days": 0,
        "longest_streak": 0,
        "streak_bonus_date": None,
        "last_active_date": None,
        "freezes": 0,
        "freeze_progress": 0,
        "achievements": [],
        "categories": {},
        "daily_highlights": 0,
        "highlight_date": None,
        "processed": [],
        "processed_dates": {},
        "last_poll": None,
    }


def _strip_transient(state: dict) -> None:
    """Drop the per-call scratch keys (``_bonus``, ``_leveled``, ...) before
    a write. They exist only to hand a value from a helper back to cmd_poll
    within one call; persisting them let ``_leveled`` grow forever across
    polls (it was being read back and appended to on the next run) and leaked
    into ``--ledger`` output. Everything a caller needs comes back in the
    payload dict instead."""
    for key in [k for k in state if k.startswith("_")]:
        del state[key]


def _prune_processed(state: dict, backend_name: str, today: date, since) -> None:
    """Shrink old dedupe keys without evicting the backend's overlap window.

    A high-volume day may temporarily exceed SEEN_CAP. Correctness wins over a
    hard cap: keys are removable only after shipped backends can no longer
    return those records. Every shipped backend filters by the ``since``
    watermark this poll passed (Todoist server-side; markdown/json client-side,
    the latter with a one-day grace), so the safe cutoff is derived from
    ``since`` itself — NOT from a fixed keep-days window, which would evict
    keys for records a long-gap poll can still return (user offline a week,
    then exceeding SEEN_CAP: a fixed cutoff would re-award days 3+)."""
    keys = list(state.get("processed", []))
    if len(keys) <= SEEN_CAP or backend_name not in {"todoist", "markdown", "json"}:
        return
    cutoff = None
    if since:
        try:
            # Records returned by this poll completed on or after since's
            # calendar day (markdown) or the day before it (json grace) —
            # prune strictly before that, for every backend.
            cutoff = datetime.fromisoformat(str(since).replace("Z", "+00:00")).date()
        except ValueError:
            cutoff = None
    if cutoff is None:
        return
    cutoff -= timedelta(days=1)
    dates = state.setdefault("processed_dates", {})
    removable = {
        key for key in keys
        if dates.get(key) and date.fromisoformat(dates[key]) < cutoff
    }
    remove_count = min(len(removable), len(keys) - SEEN_CAP)
    kept = []
    for key in keys:
        if remove_count and key in removable:
            dates.pop(key, None)
            remove_count -= 1
            continue
        kept.append(key)
    state["processed"] = kept


def cmd_poll(cfg: dict, state: dict, ledger_path: Path) -> dict:
    """Fetch completions, award XP, persist. Never prints — returns a payload
    dict so the caller (text renderer, --json, or a future non-CLI embedder)
    decides what, if anything, to show."""
    if not cfg.get("active", True):
        return {"active": False, "baseline": False, "batch": []}  # paused: no awards, no state churn

    backend = load_backend(cfg["backend"])
    tzname = cfg.get("timezone", "UTC")
    today = local_today(tzname)

    since = state.get("baseline_at")
    if not since:
        # First run: START FRESH, visibly. Record what already exists so years
        # of history never flood the ledger with unearned XP, and say so once
        # rather than silently ignoring it or silently awarding it.
        existing = [normalize_record(r) for r in backend.list_completions("", cfg)]
        existing = [r for r in existing if r is not None]
        state["processed"] = [dedupe_key(r, tzname) for r in existing]
        state["processed_dates"] = {
            dedupe_key(r, tzname): completion_date(r, tzname).isoformat()
            for r in existing
        }
        state["baseline_at"] = now_utc().isoformat()
        state["last_poll"] = now_utc().isoformat()
        atomic_write_json(ledger_path, state)
        return {"active": True, "baseline": True, "baseline_count": len(existing), "batch": []}

    records = [normalize_record(raw) for raw in backend.list_completions(since, cfg)]
    dated_records = []
    for rec in records:
        if rec is None:
            continue
        completed_on = completion_date(rec, tzname)
        if completed_on > today:
            continue
        dated_records.append((completed_on, rec))
    dated_records.sort(key=lambda pair: (pair[0], pair[1]["completed_at"], pair[1]["id"]))
    # An ordered list, not just a set: SEEN_CAP truncation below must evict the
    # OLDEST keys. A set has no reliable iteration order, so capping straight
    # off `set(...)` could evict a key added this very poll and re-admit an
    # ancient one — silently letting a task double-pay once it cycles back in.
    seen_list = list(state.get("processed", []))
    seen_set = set(seen_list)
    processed_dates = state.setdefault("processed_dates", {})
    batch = []
    leveled: list[int] = []
    for completed_on, rec in dated_records:
        key = dedupe_key(rec, tzname)
        if key in seen_set:
            continue
        seen_set.add(key)
        seen_list.append(key)
        processed_dates[key] = completed_on.isoformat()
        xp = score(rec, cfg.get("xp", DEFAULT_XP))
        apply_completion(state, xp, completed_on, rec["category"])
        # apply_completion reports level-ups from THIS completion only; collect
        # them here rather than letting a later completion in the same batch
        # overwrite them, or a level-up from completion #1 of 5 vanishes.
        leveled.extend(state.get("_leveled") or [])
        batch.append({"title": rec["title"], "xp": xp, "bonus": state.get("_bonus", 0)})

    newly = []
    emphasis: list[str] = []
    if batch:
        state["processed"] = seen_list
        _prune_processed(state, cfg["backend"], today, since)
        # Achievement bonuses can themselves roll into a level, so run the
        # level pass again and append anything it produced.
        newly = check_achievements(state)
        leveled.extend(_advance_levels(state))
        # Rationed emphasis: a level-up outranks a new achievement, and the cap
        # means the third thing today reports plainly instead of shouting.
        if leveled and take_highlight(state, today):
            emphasis.append(f"🎉 LEVEL UP — Level {leveled[-1]}!")
        for label, bonus in newly:
            if take_highlight(state, today):
                emphasis.append(f"🏅 {label} unlocked!  +{bonus} XP")
    state["baseline_at"] = now_utc().isoformat()
    state["last_poll"] = now_utc().isoformat()
    _strip_transient(state)
    atomic_write_json(ledger_path, state)

    achievement_bonus = sum(b for _label, b in newly)
    gained = sum(b["xp"] + b.get("bonus", 0) for b in batch) + achievement_bonus
    bonus_total = sum(b.get("bonus", 0) for b in batch)
    return {
        "active": True,
        "baseline": False,
        "batch": batch,
        "gained": gained,
        "bonus_total": bonus_total,
        "achievement_bonus": achievement_bonus,
        "unlocked": [{"label": label, "bonus": bonus} for label, bonus in newly],
        "leveled_to": leveled,
        "emphasis": emphasis,
        "level": state["level"],
        "xp_into_level": state.get("xp_into_level", 0),
        "xp_to_next_level": xp_to_next(state["level"]),
        "streak_days": state.get("streak_days", 0),
        "longest_streak": state.get("longest_streak", 0),
        "freezes": state.get("freezes", 0),
        "notify": cfg.get("notify", "digest"),
    }


def cmd_streak_check(cfg: dict, state: dict) -> dict:
    """Evening defense: warn BEFORE the streak is lost, not after.

    Read-only — writes nothing, since it never changes the ledger's own
    numbers (see effective_streak: that correction lands for real on the
    next completion, not here).
    """
    today = local_today(cfg.get("timezone", "UTC"))
    if not cfg.get("active", True):
        return {"active": False, "warning": None}
    streak_days, protected = effective_streak(state, today)
    if state.get("last_active_date") == today.isoformat() or streak_days <= 0:
        return {"active": True, "warning": None, "streak_days": streak_days, "freezes": state.get("freezes", 0)}
    freezes = state.get("freezes", 0)
    if protected and freezes > 0:
        warning = f"⚠️ No completions today. Streak {streak_days}d saved by a freeze ({freezes} left)."
    else:
        warning = f"⚠️ No completions today — your {streak_days}-day streak ends at midnight."
    return {"active": True, "warning": warning, "streak_days": streak_days, "freezes": freezes}


def validate_config(cfg: dict) -> list[str]:
    """Return actionable config errors instead of allowing later KeyErrors."""
    errors = []
    if not cfg.get("ledger"):
        errors.append("missing ledger path")
    backend = cfg.get("backend")
    if backend not in {"todoist", "markdown", "json"}:
        errors.append(f"unsupported backend: {backend!r}")
    if not isinstance(cfg.get("backend_options"), dict):
        errors.append("backend_options must be an object")
    tzname = cfg.get("timezone", "UTC")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tzname)
    except (KeyError, TypeError, ValueError):
        errors.append(f"invalid timezone: {tzname!r}")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="rewards.py",
        description="task-rewards \u2014 XP, levels and streaks for completed tasks",
        epilog="First time? Run --setup. Something broken? Run --doctor.",
    )
    ap.add_argument("--config", default=str(profile_paths.default_config_path()),
                    help="path to config JSON (default: $HERMES_HOME/task-rewards.json, "
                         "else ~/.hermes/task-rewards.json)")
    ap.add_argument("--poll", action="store_true", help="fetch completions and award")
    ap.add_argument("--status", action="store_true", help="print player status")
    ap.add_argument("--compact", action="store_true", help="short status")
    ap.add_argument("--streak-check", action="store_true", help="evening streak warning")
    ap.add_argument("--ledger", action="store_true", help="dump raw ledger as JSON")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output for --poll/--status/--streak-check, "
                         "for driving this from a bot other than the Hermes agent")

    setup_grp = ap.add_argument_group("setup")
    setup_grp.add_argument("--setup", action="store_true",
                           help="run the interactive setup wizard")
    setup_grp.add_argument("--auto", action="store_true",
                           help="with --setup, fully non-interactive install-time detection: "
                                "Todoist if a token is already configured, else the first "
                                "existing checklist found, else a fresh blank one")
    setup_grp.add_argument("--doctor", action="store_true",
                           help="report the environment and exit (read-only)")
    setup_grp.add_argument("--list-projects", action="store_true",
                           help="list Todoist projects and exit")
    setup_grp.add_argument("--force", action="store_true",
                           help="with --setup, overwrite an existing config")
    setup_grp.add_argument("--yes", "-y", action="store_true",
                           help="with --setup, accept detected defaults")
    setup_grp.add_argument("--backend", choices=["todoist", "markdown", "json"],
                           help="with --setup, skip source detection")
    setup_grp.add_argument("--project-id", help="with --setup, the Todoist project")
    setup_grp.add_argument("--path", help="with --setup, the markdown file or folder")
    setup_grp.add_argument("--scope", help="with --setup, ledger name")
    setup_grp.add_argument("--category", help="with --setup, category for ladders")
    setup_grp.add_argument("--timezone", help="with --setup, IANA timezone")
    setup_grp.add_argument("--ledger-path", dest="ledger_file",
                           help="with --setup, where to write the ledger")
    setup_grp.add_argument("--notify", choices=["digest", "instant", "off"],
                           help="with --setup, notification style")

    args = ap.parse_args(argv)
    cfg_path = Path(os.path.expanduser(args.config))

    # Commands that do not need an existing config.
    if args.setup or args.doctor or args.list_projects:
        import wizard
        if args.list_projects:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from backends import todoist
            try:
                for p in todoist.list_projects():
                    print(f"{p.get('id')}  {p.get('name')}")
            except Exception as exc:  # noqa: BLE001
                print(f"task-rewards: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 2
            return 0
        if args.doctor:
            return wizard.cmd_doctor(cfg_path)
        return wizard.cmd_setup(args)

    cfg = load_json(cfg_path, {})
    if not cfg:
        print(f"task-rewards: no config at {str(cfg_path).replace(str(Path.home()), '~', 1)}",
              file=sys.stderr)
        others = profile_paths.discover_configs()
        if others:
            print("  found existing config(s):", file=sys.stderr)
            for p in others:
                print(f"    --config {str(p).replace(str(Path.home()), '~', 1)}",
                      file=sys.stderr)
        me = str(Path(__file__)).replace(str(Path.home()), "~", 1)
        print(f"  or run:  python3 {me} --setup", file=sys.stderr)
        return 2

    config_errors = validate_config(cfg)
    if config_errors:
        print(f"task-rewards: invalid config at {cfg_path}", file=sys.stderr)
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        print("  re-run --setup --force to repair it", file=sys.stderr)
        return 2

    if not (args.ledger or args.status or args.streak_check or args.poll):
        ap.print_help()
        return 1

    ledger_path = Path(os.path.expanduser(cfg["ledger"]))
    scope = cfg.get("scope", "default")

    lock_path = ledger_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lf:
        release = _lock_handle(lf)
        try:
            # Loaded AFTER the lock is held, not before: two overlapping
            # --poll runs (a slow API call plus an unlucky cron overlap)
            # must not let the second one act on a stale in-memory copy and
            # clobber the first one's write when it releases the lock.
            state = load_json(ledger_path, default_state(scope))
            recovered_from = state.pop("_recovered_from", None)
            recovery_warning = None
            if recovered_from:
                recovery_warning = f"Recovered from corrupt ledger; backup: {recovered_from}"
                print(f"task-rewards: {recovery_warning}", file=sys.stderr)

            if args.ledger:
                print(json.dumps(state, indent=2, sort_keys=True))
                return 0

            today = local_today(cfg.get("timezone", "UTC"))
            results: dict[str, dict] = {}
            if args.poll:
                results["poll"] = cmd_poll(cfg, state, ledger_path)
            if args.status:
                results["status"] = status_payload(state, scope, today)
            if args.streak_check:
                results["streak_check"] = cmd_streak_check(cfg, state)
            if recovery_warning:
                for payload in results.values():
                    payload["recovery_warning"] = recovery_warning

            if args.json:
                out = next(iter(results.values())) if len(results) == 1 else results
                print(json.dumps(out, indent=2, sort_keys=True))
                return 0

            texts = []
            if "poll" in results:
                pr = results["poll"]
                if pr["active"] and pr["baseline"]:
                    texts.append(f"Baseline set \u2014 {pr['baseline_count']} existing completion(s) "
                                 "recorded, no XP awarded. Rewards start from now.")
                elif pr["active"] and pr.get("notify") != "off":
                    texts.append(render_poll(pr))
            if "status" in results:
                texts.append(render_status(results["status"], args.compact))
            if "streak_check" in results:
                sr = results["streak_check"]
                if sr["active"] and sr["warning"]:
                    texts.append(sr["warning"])
            for text in texts:
                if text:
                    print(text)
            return 0
        finally:
            release()


if __name__ == "__main__":
    raise SystemExit(main())
