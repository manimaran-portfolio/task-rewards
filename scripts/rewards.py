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

VERSION = 1
DEFAULT_XP = {"4": 50, "3": 30, "2": 20, "1": 10}
FREEZE_EVERY = 7
FREEZE_CAP = 2
SEEN_CAP = 500  # bound the dedupe set; history is truncated on free tiers anyway
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


def dedupe_key(rec: dict) -> str:
    """task identity at day granularity.

    Keying on the raw timestamp would re-award when a user unchecks and
    rechecks a task (Todoist issues a fresh completed_at). Day granularity
    absorbs that, and lets a recurring task legitimately pay once per day.
    """
    stamp = str(rec.get("completed_at") or "")[:10]
    basis = f"{rec.get('source','')}:{rec.get('id','')}:{stamp}"
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
    if last == today_iso:
        pass
    elif last == (today - timedelta(days=1)).isoformat() or last is None:
        state["streak_days"] = state.get("streak_days", 0) + 1
        state["freeze_progress"] = state.get("freeze_progress", 0) + 1
        if state["freeze_progress"] >= FREEZE_EVERY:
            state["freezes"] = min(FREEZE_CAP, state.get("freezes", 0) + 1)
            state["freeze_progress"] = 0
    else:
        # gap: consume a freeze if banked, else reset
        if state.get("freezes", 0) > 0:
            state["freezes"] -= 1
            state["streak_days"] = state.get("streak_days", 0) + 1
        else:
            state["streak_days"] = 1
    state["last_active_date"] = today_iso
    state["longest_streak"] = max(state.get("longest_streak", 0), state["streak_days"])

    # Flat once-per-day streak bonus, paid on the first completion of the day.
    # Deliberately NOT a per-task multiplier: at x2, clearing 15 trivial
    # subtasks would bank 30 tasks' worth of XP for one day of consistency.
    bonus = 0
    if state.get("streak_bonus_date") != today_iso:
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


def expire_streak(state: dict, today: date) -> str | None:
    """Detect a missed day. Returns an advisory string, or None."""
    last = state.get("last_active_date")
    if not last or state.get("streak_days", 0) == 0:
        return None
    try:
        last_d = date.fromisoformat(last)
    except ValueError:
        return None
    gap = (today - last_d).days
    if gap <= 1:
        return None
    if state.get("freezes", 0) > 0:
        return f"Streak saved by a freeze ({state['freezes']} left)."
    if state.get("streak_days", 0) > 0:
        state["streak_days"] = 0
        # Framed as a beginning, never a loss. Punishment framing is what makes
        # people quit a streak system outright.
        return "A new streak starts today."
    return None


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
    have = set(state.get("achievements", []))
    out = []
    for key, label, _pred, target, _bonus in all_achievements(state):
        out.append((key, label, key in have, _progress(state, key, target), target))
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


def render_poll(state: dict, batch: list[dict]) -> str:
    """The completion ping. Empty string when there is nothing to say."""
    if not batch:
        return ""
    gained = sum(b["xp"] + b.get("bonus", 0) for b in batch)
    bonus_total = sum(b.get("bonus", 0) for b in batch)
    gained += state.get("_achievement_bonus", 0)
    lines = [f"⚡ +{gained} XP", ""]
    for b in batch[:8]:
        lines.append(f"{b['title'][:38]:<38} +{b['xp']}")
    if bonus_total:
        lines.append(f"{'🔥 daily streak bonus':<38} +{bonus_total}")
    if len(batch) > 8:
        lines.append(f"...and {len(batch) - 8} more")
    # The legs of the ladder that were rolled into levels, then the streak
    # milestone. Only a rationed subset gets an emphasised line.
    lines.append("")
    for line in (state.get("_emphasis") or []):
        lines.append(line)
    need = xp_to_next(state["level"])
    if need:
        lines.append(f"🏆 Level {state['level']}  {bar(state['xp_into_level'], need)}  {state['xp_into_level']}/{need} XP")
    else:
        lines.append(f"🏆 Level {state['level']} — MAX")
    lines.append(f"🔥 Streak {state['streak_days']}d (best {state.get('longest_streak', 0)}d)   🛡️ {state.get('freezes', 0)} freeze")
    return "\n".join(lines)


def render_status(state: dict, scope: str, compact: bool = False) -> str:
    need = xp_to_next(state["level"])
    level_line = (
        f"Level {state['level']}   {bar(state['xp_into_level'], need)}   {state['xp_into_level']}/{need} XP"
        if need else f"Level {state['level']} — MAX"
    )
    head = [
        f"🏆 Rewards — {scope}",
        "",
        level_line,
        f"🔥 Streak {state['streak_days']}d (best {state.get('longest_streak', 0)}d)   🛡️ {state.get('freezes', 0)} freeze banked",
        f"📊 {state.get('tasks_completed', 0)} tasks · {state.get('total_xp', 0)} XP lifetime",
    ]
    if compact:
        return "\n".join(head[:3])
    achs = earned_achievements(state)
    got = sum(1 for a in achs if a[2])
    head += ["", f"Achievements   {got} / {len(achs)}"]
    for _key, label, unlocked, cur, target in achs:
        if unlocked:
            head.append(f"✅ {label}")
        else:
            head.append(f"🔒 {label}  ({cur}/{target})")
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
        "last_poll": None,
    }


def cmd_poll(cfg: dict, state: dict, ledger_path: Path, quiet: bool) -> str:
    if not cfg.get("active", True):
        return ""  # paused: no awards, no notifications, no state churn

    backend = load_backend(cfg["backend"])
    today = local_today(cfg.get("timezone", "UTC"))

    since = state.get("baseline_at")
    if not since:
        # First run: START FRESH, visibly. Record what already exists so years
        # of history never flood the ledger with unearned XP, and say so once
        # rather than silently ignoring it or silently awarding it.
        existing = [normalize_record(r) for r in backend.list_completions("", cfg)]
        existing = [r for r in existing if r is not None]
        state["processed"] = [dedupe_key(r) for r in existing][-SEEN_CAP:]
        state["baseline_at"] = now_utc().isoformat()
        state["last_poll"] = now_utc().isoformat()
        atomic_write_json(ledger_path, state)
        if quiet:
            return ""
        return (
            f"Baseline set — {len(existing)} existing completion(s) recorded, "
            "no XP awarded. Rewards start from now."
        )

    records = backend.list_completions(since, cfg)
    seen = set(state.get("processed", []))
    batch = []
    for raw in records:
        rec = normalize_record(raw)
        if rec is None:
            continue  # malformed backend output must never reach the scoring path
        key = dedupe_key(rec)
        if key in seen:
            continue
        seen.add(key)
        xp = score(rec, cfg.get("xp", DEFAULT_XP))
        apply_completion(state, xp, today, rec["category"])
        batch.append({"title": rec["title"], "xp": xp, "bonus": state.get("_bonus", 0)})

    if batch:
        state["processed"] = list(seen)[-SEEN_CAP:]
        # Achievement bonuses can themselves roll into a level, so run the
        # level pass again and append anything it produced.
        newly = check_achievements(state)
        state["_leveled"] = (state.get("_leveled") or []) + _advance_levels(state)
        # Rationed emphasis: a level-up outranks a new achievement, and the cap
        # means the third thing today reports plainly instead of shouting.
        emphasis = []
        if state.get("_leveled") and take_highlight(state, today):
            emphasis.append(f"🎉 LEVEL UP — Level {state['_leveled'][-1]}!")
        for label, bonus in newly:
            if take_highlight(state, today):
                emphasis.append(f"🏅 {label} unlocked!  +{bonus} XP")
        state["_emphasis"] = emphasis
        state["_achievement_bonus"] = sum(b for _label, b in newly)
    state["baseline_at"] = now_utc().isoformat()
    state["last_poll"] = now_utc().isoformat()
    atomic_write_json(ledger_path, state)

    if quiet or cfg.get("notify") == "off":
        return ""
    return render_poll(state, batch)


def cmd_streak_check(cfg: dict, state: dict, ledger_path: Path) -> str:
    """Evening defense: warn BEFORE the streak is lost, not after."""
    if not cfg.get("active", True):
        return ""
    today = local_today(cfg.get("timezone", "UTC"))
    if state.get("last_active_date") == today.isoformat():
        return ""
    if state.get("streak_days", 0) <= 0:
        return ""
    freezes = state.get("freezes", 0)
    if freezes > 0:
        return f"⚠️ No completions today. Streak {state['streak_days']}d saved by a freeze ({freezes} left)."
    return f"⚠️ No completions today — your {state['streak_days']}-day streak ends at midnight."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="task-rewards engine")
    ap.add_argument("--config", required=True, help="path to config JSON")
    ap.add_argument("--poll", action="store_true", help="fetch completions and award")
    ap.add_argument("--status", action="store_true", help="print player status")
    ap.add_argument("--compact", action="store_true", help="short status")
    ap.add_argument("--streak-check", action="store_true", help="evening streak warning")
    ap.add_argument("--ledger", action="store_true", help="dump raw ledger")
    args = ap.parse_args(argv)

    cfg_path = Path(os.path.expanduser(args.config))
    cfg = load_json(cfg_path, {})
    if not cfg:
        print(f"task-rewards: no config at {cfg_path}", file=sys.stderr)
        return 2

    ledger_path = Path(os.path.expanduser(cfg["ledger"]))
    state = load_json(ledger_path, default_state(cfg.get("scope", "default")))

    lock_path = ledger_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lf:
        release = _lock_handle(lf)
        try:
            if args.ledger:
                print(json.dumps(state, indent=2, sort_keys=True))
                return 0
            if args.status:
                print(render_status(state, cfg.get("scope", "default"), args.compact))
                return 0
            if args.streak_check:
                msg = cmd_streak_check(cfg, state, ledger_path)
                if msg:
                    print(msg)
                    atomic_write_json(ledger_path, state)
                return 0
            if args.poll:
                out = cmd_poll(cfg, state, ledger_path, quiet=args.status)
                if out:
                    print(out)
                return 0
        finally:
            release()

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
