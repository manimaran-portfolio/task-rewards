# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Hermes Agent skill (`SKILL.md`) that adds a local XP/levels/streaks reward layer on top of a task
list the user already uses (Todoist or a markdown checklist). It is not a web app or service — it's a
stdlib-only Python CLI plus a skill manifest that tells an LLM agent how to drive that CLI.

Read `README.md` for the mechanics/design rationale and `SKILL.md` for the agent-facing procedure
(setup flow, division of labour, pitfalls) — both are detailed and authoritative; don't duplicate their
content in code comments.

## Commands

No build step, no package manager, no test runner — stdlib only, Python 3.9+.

```bash
# First-run setup: detects a task source, writes a config, runs a no-op baseline
python3 scripts/rewards.py --setup
python3 scripts/rewards.py --setup --yes --project-id <id> --scope health   # non-interactive
python3 scripts/rewards.py --setup --list-projects                          # list Todoist projects

# Zero-touch install (what a Hermes install hook should run): Todoist if a
# token is already configured, else an existing checklist, else a blank one
python3 scripts/rewards.py --setup --auto

# Environment diagnosis — read-only, run this first when anything fails
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --doctor

# Day-to-day
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --poll          # award XP (silent if nothing happened)
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --status        # level/streak/achievements
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --status --compact
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --streak-check  # evening streak-loss warning
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --ledger        # dump raw ledger JSON
python3 scripts/rewards.py --config ~/.hermes/task-rewards.json --poll --status --json  # bot-facing, one call

# Tests
python3 -m unittest discover -s tests -v
```

`--config` (and every other default path) resolves under `$HERMES_HOME` when set, else `~/.hermes` —
see `scripts/profile_paths.py`. Tests are stdlib `unittest`, no runner to install; a handful invoke
`scripts/rewards.py` as a real subprocess for end-to-end coverage (see `tests/test_cli_integration.py`,
`tests/test_wizard.py`), the rest import the modules directly. Run the whole suite before any change to
`scripts/rewards.py`, `scripts/wizard.py`, or a backend — the ledger-lock, dedupe-cap, and duplicate-task
identity tests exist specifically because those bugs were silent (no exception, no failing assertion)
until exercised the right way.

## Architecture

**Engine vs. agent split, enforced by file boundaries.** `scripts/rewards.py` is the deterministic state
authority — the only code that ever computes XP, mutates the ledger, or decides a streak/achievement.
It is stdlib-only and has no idea what an LLM is. The Hermes agent invoking this skill owns *meaning*
(status commentary, coaching, briefings) and must never compute or edit ledger numbers itself. Don't
blur this: new features that need "judgment" belong in the agent's prompt/skill instructions, not in
`rewards.py`, and anything that touches XP/levels/streaks must go through the engine.

**Backend contract (`scripts/backends/`)** — a backend is one function:
```python
def list_completions(since_iso: str, cfg: dict) -> list[dict]:
    return [{"id": str, "title": str, "completed_at": ISO8601,
             "priority": "1".."4", "category": str, "source": str}]
```
`rewards.load_backend()` imports `backends.<name>` dynamically by the `backend` key in the config —
the module's filename must exactly match that name (`backends/json.py`, not `jsonl.py`; this was a real
bug where `--backend json` was unimportable for every user until the file was renamed).
`normalize_record()` in `rewards.py` then strips every record down to exactly
`{id, title, completed_at, priority, category, source}` — this is the ingest contract, and no backend
may widen it. Malformed records are dropped rather than raising, so one bad record never corrupts the
ledger. Three backends exist: `todoist.py` (REST API, credential lookup below), `markdown.py` (parses
Obsidian Tasks / todo.txt / plain checkbox conventions in place, numbering same-text duplicates by a
stable `(completed_at, file, line)` order rather than raw scan order so an unrelated edit can't shift an
already-rewarded task's identity), `json.py` (JSON array or NDJSON).

**Dedup and idempotency** — `dedupe_key()` hashes `source:id:completed_at[:10]` (day granularity, not
the raw timestamp) so unchecking/rechecking a task in Todoist (which issues a fresh `completed_at`)
doesn't re-award, while a genuinely recurring task can still pay once per day. `state["processed"]` is
the seen-set, capped at `SEEN_CAP` (500) entries — kept as an ORDER-PRESERVING list in `cmd_poll()`
(append-then-truncate-from-the-front), not derived from a plain `set`, because sets have no reliable
iteration order and capping off one can evict a key from the poll that just ran instead of a genuinely
old one.

**Baseline-on-first-run** — the first `--poll` for a new ledger never awards XP; it just records every
existing completion as already-seen (`state["baseline_at"]`) so years of task history don't flood in.
Preserve this behavior in any change to `cmd_poll()`.

**Unlock vs. render separation** — `check_achievements()` is the only place an achievement is unlocked
and its bonus paid (append-only to `state["achievements"]`). `earned_achievements()` (used by
`status_payload()`) is a pure read view that re-evaluates every achievement predicate against the live
counters on every call — achievements are always derived on demand from `tasks_completed`/`total_xp`/
`streak_days`/`categories`, never replayed from a cached "unlocked" flag. Never let a display/render path
call anything that mutates state — that's how a bonus gets paid twice.

**`cmd_poll()`/`cmd_streak_check()` return payload dicts, never print.** `main()` decides whether to
render text or `--json`. Before persisting, `cmd_poll()` calls `_strip_transient()` to drop every
`_`-prefixed scratch key (`_bonus`, `_leveled`, ...) — these exist only to hand a value back within one
call; leaving them in the ledger let one of them (`_leveled`) grow unbounded across polls in the old code.

**`effective_streak(state, today)`** is a pure, non-mutating view used by every renderer and
`cmd_streak_check()` instead of reading `state["streak_days"]` directly. The ledger's own `streak_days`
is only ever corrected for real by `apply_completion()` on the *next* completion — there's no job that
walks in and zeroes a stale streak after several silent days — so display must go through this or it can
report a streak as alive days after it actually broke.

**Storage** — one JSON ledger per scope, written via `atomic_write_json()` (temp file + fsync + atomic
`os.replace`) under an advisory file lock (`fcntl`/`msvcrt`, best-effort cross-platform) taken on
`<ledger>.lock`. **`main()` loads ledger state only after acquiring that lock**, not before — loading
first (the old bug) let two overlapping invocations each act on a stale in-memory copy and have the
second one's write silently clobber the first's. A corrupt ledger is moved aside
(`*.corrupt.<timestamp>.json`) rather than crashing. Config and ledger are separate files; ledgers/configs
are gitignored (`templates/config.example.json` is the only committed example).

**Profile-aware paths (`scripts/profile_paths.py`)** — every default path (config, ledger, blank-checklist
target, config discovery) resolves under `$HERMES_HOME` when set, else `~/.hermes`. This is the single
source of truth; `backends/todoist.py`'s `.env` lookup goes through the same `hermes_home()` rather than
re-deriving it, so a second Hermes profile gets its own config/ledger/credentials automatically.

**Credential lookup (`backends/todoist.py`)** — `TODOIST_API_TOKEN` is never stored in the config. It's
resolved from the filesystem (`env_candidates()`: `$HERMES_HOME/.env`, then `~/.hermes/.env`, then every
`~/.hermes/profiles/*/.env`), never solely from `os.environ`, because a cron invocation has no shell
profile and Hermes profiles each keep their own `.env`. `--doctor` reports which file it found the token
in, never the token itself.

**Setup wizard (`scripts/wizard.py`)** — `cmd_setup()` and `cmd_doctor()` are the only entry points; both
work interactively (TTY prompts) and non-interactively (flags + `--yes`), since an agent drives this
without a terminal attached. `--setup --auto` (`_auto_detect()`) is the unattended install-time path:
Todoist-if-configured → `_scan_for_checklist()` over `COMMON_CHECKLIST_ROOTS` → a fresh blank checklist
at `profile_paths.default_checklist_path()`, always ending active. `wizard.py` has no `main()` —
`rewards.py` dispatches into it for `--setup`/`--doctor`/`--list-projects` before requiring a config to
exist.

## Working in this repo

- Keep `rewards.py` stdlib-only — no third-party dependencies, per the project's core design promise.
  Tests follow the same rule (`unittest`, not `pytest`).
- Any new mechanic (multipliers, bonuses, etc.) should be checked against the "Design notes" section of
  `README.md` and "Pitfalls" in `SKILL.md` first — several plausible-looking features (stacking
  multipliers, a due-date bonus, a manual XP grant) were deliberately rejected there, with reasons that
  still apply.
- `--poll` and `--streak-check` must stay silent (empty stdout, in text mode) when there's nothing to
  report — they're meant to run unattended on cron every 15 minutes. `--json` always emits a payload
  object even when empty; silence is a text-rendering concern, not an engine one.
- `backend_options["category"]` is read only by the Todoist backend (`markdown`/`json` derive a category
  from each record itself) — don't write it into a config for another backend; it would look meaningful
  and silently do nothing.
