# task-rewards

A gamification layer for task lists you **already use**. Completed tasks pay XP,
XP earns levels, days you finish something protect a streak, and achievements
mark the milestones.

It does not replace your task manager. It reads it.

- **Deterministic** — a stdlib Python engine owns the scoring. Same input, same
  output, no LLM in the loop, zero tokens per check.
- **Idempotent** — polling twice never double-awards.
- **Cron-safe** — silent when nothing happened.
- **Local** — one JSON ledger on your disk. Nothing leaves the machine.
- **Backends** — Todoist, or any markdown checklist.

## Why not an LLM-mediated tracker

Several gamification skills drive the whole system from a prompt: *"you are the
XP engine."* That works for a demo and rots over weeks — counters desynchronise,
XP gets double-entered, level caps get hallucinated. It also cannot run on a
15-minute schedule, because each check costs tokens to discover that nothing
happened.

This splits the job:

| | Owns |
|---|---|
| **Engine** (Python) | the numbers — ingest, dedupe, score, persist |
| **Agent** (LLM) | the meaning — status queries, briefings, coaching |

The script owns the numbers; the agent owns the meaning.

## Install

As a Hermes Agent skill, drop this directory into your skills folder:

```bash
git clone <this-repo> ~/.hermes/skills/productivity/task-rewards
```

Requires Python 3.9+. There are no dependencies — standard library only.

## Quick start

```bash
# 1. write a config (see templates/config.example.json)
cp templates/config.example.json ~/.hermes/task-rewards.json

# 2. first poll establishes a baseline and awards nothing
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --poll

# 3. check in any time
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --status
```

`--poll` prints nothing when nothing was completed. Add it to cron every 15
minutes and forget about it.

## Commands

| Command | Purpose |
|---|---|
| `--poll` | Fetch completions, award XP, print a reward block (empty if none) |
| `--status` | Level, streak, totals, achievement list with progress |
| `--status --compact` | Two lines only |
| `--streak-check` | Evening warning before a streak is lost (empty if safe) |
| `--ledger` | Dump the raw ledger as JSON |

## Backends

| Backend | Source | Category from |
|---|---|---|
| `todoist` | Todoist REST completed-tasks API | `backend_options.category` |
| `markdown` | Obsidian Tasks, todo.txt, plain checkboxes | first non-marker `#tag`, else subfolder (directory scans only) |
| `json` | Any JSON array / NDJSON file | `category` field |

The markdown backend parses conventions you already have rather than inventing
one:

```markdown
- [x] #task Ship the release ✅ 2026-09-18 🔺     ← Obsidian Tasks
x 2026-09-18 (A) Submit proposal +work           ← todo.txt
- [x] Buy milk                                    ← plain checkbox
```

- `✅ YYYY-MM-DD` and `x YYYY-MM-DD` carry a **real** completion date
- `🔺 ⏫ 🔼 🔽 ⏬` and `(A)`–`(Z)` carry **real** priorities
- Undated checkboxes are stamped at detection time — idempotency comes from the
  day-granular dedupe key

Categories come from the first real `#tag` (`#task`/`#todo`/`#done` are markers,
not classes). If there is no tag, the **subfolder** is used — but only when the
configured path is a directory, where the subfolder actually means something.
Point at a single file and untagged tasks go to `inbox` rather than inventing a
category from whatever folder that file happens to sit in.

### Writing a backend

A backend is one function:

```python
def list_completions(since_iso: str, cfg: dict) -> list[dict]:
    return [{"id": str, "title": str, "completed_at": ISO8601,
             "priority": "1".."4", "category": str, "source": str}]
```

`category` is optional — return `""` and the engine files it under `inbox`.
`since_iso` is advisory; returning extra history is safe because the engine
dedupes, but returning too little silently loses rewards. When in doubt, return
more. Everything is validated on ingest, so a malformed record is skipped rather
than allowed to corrupt the ledger.

## Mechanics

- **XP by priority** — Todoist API integer: `4 → 50, 3 → 30, 2 → 20, 1 → 10`.
  The API integer is *inverted* against the app's P1–P4 labels: API 4 is urgent.
- **Daily streak bonus** — first completion of each day pays `10 XP × tier`
  (1 under 10 days, 2 from 10, 3 from 30; 30 XP max). Paid once per day, never
  per task.
- **Levels** — advancing *from* level L costs `round(100 × L^1.5)` XP, capped at
  50. Surplus rolls over.
- **Streak** — one calendar day with ≥1 completion. Every 7 continuous days
  banks a freeze (max 2). A missed day consumes a freeze; with none banked the
  streak resets.
- **Achievements** — derived from counters, never stored by hand. Base set plus
  a per-category ladder (`Apprentice` 10, `Master` 20, `Expert` 50) generated
  for every category the ledger has seen.
- **Highlight throttle** — at most 2 emphasised messages per day, priority
  level-up > achievement > streak milestone. Past the cap, completions still
  report, just quietly.
- **`"active": false`** pauses everything silently.

## Design notes

Things that were deliberately **not** built, and why:

- **No stacking multipliers** — effort, due-date timing, or per-task streak.
  Each looks reasonable alone, but multiplied together they produce a ~38×
  spread between the worst and best single completion. A due-date multiplier
  also pays you to pad deadlines and punishes finishing at 12:05 AM.
- **No `grant XP` command** — rewards come only from real completions, or the
  numbers stop meaning anything.
- **Streak resets are never phrased as a loss** — punishment framing is what
  makes people abandon a streak system outright.
- **Unlocking is separated from rendering** — `check_achievements()` pays
  bonuses; `earned_achievements()` is read-only. If rendering could unlock,
  every `--status` would mint free levels.

## License

MIT — see [LICENSE](LICENSE).
