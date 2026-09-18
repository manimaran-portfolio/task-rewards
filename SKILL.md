---
name: task-rewards
description: Award XP, levels, and streaks for completed tasks.
version: 0.1.0
author: Mani (manimaran-portfolio), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Gamification, Tasks, Todoist, Markdown, Motivation]
    related_skills: []
---

# Task Rewards Skill

Adds a local reward layer on top of a task list you already use — it does not
replace it. Completed tasks pay XP, XP earns levels, days you finish something
protect a streak, and five derived achievements mark the milestones.

It is a plain stdlib Python engine plus swappable backends, so it works against
Todoist or against any markdown checklist. Nothing leaves the machine.

## When to Use

- The user wants task completion to feel more rewarding, or asks for XP, levels,
  streaks, or badges on their to-dos.
- The user asks "how am I doing?", "show my achievements", or "what's my streak?"
- A daily or morning briefing should include a progress block.
- Don't use for: team leaderboards, points that other people can see or award, or
  replacing a task manager. This is single-user and local by design.

## Prerequisites

- Python 3.9+ on PATH (stdlib only — no packages to install).
- A task source:
  - **markdown** — a file or folder of checklists. No account needed.
  - **todoist** — requires `TODOIST_API_TOKEN` in the environment. Read it from
    `$HERMES_HOME/.env` (falling back to `~/.hermes/.env`); never write the
    token into the config file.
  - **json** — a JSON array or NDJSON file of completion records.
- Optional: a messaging platform configured in Hermes, for pushing rewards.

## Installation

Run `--setup --auto` once, right after this skill's files land in a profile's
skills directory — it needs no arguments and no TTY:

```
terminal(command="python3 ${SKILL_DIR}/scripts/rewards.py --setup --auto")
```

It cascades: an already-configured Todoist token wins (picks the "Inbox"
project, or the first one, without asking which); otherwise it scans common
checklist locations (`~/Documents`, `~/Obsidian`, `~/notes`, `~/.hermes`, ...)
for a file that already contains real `- [ ]`/`- [x]`/todo.txt lines and
adopts it; otherwise it creates a fresh blank checklist and uses that. Either
way the install ends **active**, never stuck waiting on a question. All of
this respects `$HERMES_HOME`, so installing into a second profile (a health
bot, a finance bot, ...) gets its own config/ledger automatically — nothing
to pass by hand.

If the auto-detected source is wrong for this user (wrong Todoist project, or
it adopted the wrong notes folder), re-run interactively with `--force` and
the flags in **Setup and Diagnosis** below to override it — auto-detection is
a good-enough default, not a promise that it read the user's mind.

## How to Run

All commands go through `terminal`. `${SKILL_DIR}` is this skill's directory.

```
terminal(command="python3 ${SKILL_DIR}/scripts/rewards.py --setup")
terminal(command="python3 ${SKILL_DIR}/scripts/rewards.py --config ~/.hermes/task-rewards.json --poll")
terminal(command="python3 ${SKILL_DIR}/scripts/rewards.py --config ~/.hermes/task-rewards.json --status")
```

`--poll` is silent when nothing changed by design, so it is safe on a schedule.
If anything looks wrong, run `--doctor` before debugging by hand.

## Setup and Diagnosis

**`--setup` is the entry point, not the config file.** It detects the
environment, lists real task sources, writes a config, and runs a baseline
dry-run that awards nothing. It works interactively on a TTY and
non-interactively via flags, so you can drive it without prompting:

```
terminal(command="python3 ${SKILL_DIR}/scripts/rewards.py --setup --yes")
terminal(command="python3 ${SKILL_DIR}/scripts/rewards.py --setup --list-projects")
terminal(command="python3 ${SKILL_DIR}/scripts/rewards.py --setup --yes --project-id <id> --scope health")
```

Flags: `--backend`, `--project-id`, `--path`, `--scope`, `--category`,
`--timezone`, `--ledger-path`, `--notify`, `--force` (overwrite), `--yes`
(accept detected defaults).

**`--doctor` reports the environment and writes nothing.** Run it first when
something fails — it answers the questions that actually go wrong:
it finds the token and names the file it came from, checks the config and
ledger are reachable, verifies a live API call, and **detects when the skill is
installed outside the active profile's skills directory** (the agent then
cannot discover it, even though every file is present). Never prints the token.

## Division of Labour — what the agent does vs what the engine does

This split is the whole design. Get it wrong in either direction and the skill
falls apart.

**The engine (deterministic Python) is the state authority.** It ingests
completions, dedupes, scores, and updates the ledger. It is the only writer.
Never let an LLM compute XP, judge a streak, or edit the ledger by hand —
models drift on arithmetic and silently corrupt counters over weeks.

**The agent (you) is the interface and the coach.** The engine cannot
congratulate anyone, notice that a goal has gone quiet, or answer a
free-text question. That is your job, and it is why this is a skill rather
than a cron script:

- Answer natural-language status questions by running `--status` and talking
  about it, not by recalling numbers from earlier in the conversation.
- Weave the reward block into the morning briefing.
- Deliver the evening streak-defence warning in your own words.
- On a level-up or unlock, say something contextual about what was actually
  achieved. Read the completion that triggered it.
- Notice when someone has stopped completing things and say so gently — the
  adaptive-difficulty case. Do not let a silent ledger go unmentioned for weeks.

The rule: **the script owns the numbers; you own the meaning.**

## Quick Reference

| Command | Purpose |
|---|---|
| `--setup --auto` | **Install time.** Zero-touch: Todoist → existing checklist → blank checklist |
| `--setup` | Detect, write config, baseline dry-run (interactive or flag-driven) |
| `--doctor` | Report the environment; read-only, writes nothing |
| `--list-projects` | List Todoist projects with their ids |
| `--poll` | Fetch completions, award XP, print a reward block (empty if none) |
| `--status` | Level, streak, totals, achievement list |
| `--status --compact` | Two lines only |
| `--streak-check` | Evening warning before a streak is lost (empty if safe) |
| `--ledger` | Dump the raw ledger as JSON |
| `--json` | Add to `--poll`/`--status`/`--streak-check` for structured output |

`--poll --status` combined runs the poll quietly and shows the resulting
status in one call. This (with `--json`) is also the integration point for
a bot that ISN'T you — this skill is a layer between the Todoist/markdown
source and whatever consumes rewards, and a Discord/Slack bot that has no
concept of "Hermes skill" can shell out to the same CLI and get a stable
JSON contract instead of parsing emoji text.

## Procedure

**First run: use `--setup`. Do not hand-build a config.** The wizard does the
detection, validation, writing and baseline in one pass.

1. **Check the environment** with `--doctor`. Fix anything it marks ✗ before
   going further — especially a skill installed outside the active profile's
   skills dir. Every file can be present and correct while the agent still
   cannot see the skill at all. Completion criterion: token found, live API
   reachable, skill visible to this profile.
2. **Choose the source with the user.** Run `--list-projects` and ask which
   project, or ask for a markdown file or folder. For markdown, read a sample
   with `read_file` first and confirm it contains *real tasks* — a reading list
   or an article summary is not a task list, and rewarding ticks in one makes
   the numbers meaningless. Completion criterion: one concrete source chosen.
3. **Run the wizard** — `--setup --yes --project-id <id> --scope <name>`. Ask
   the user for the scope name and timezone rather than inventing them.
   Completion criterion: exit 0 and a baseline notice.
4. **Confirm the baseline awarded nothing.** `--status` must show 0 XP and the
   ledger must exist. Completion criterion: 0 XP, ledger file present.
5. **Confirm, then schedule.** Only once the baseline looks right, ask whether
   to create cron jobs, then create them with `cronjob` (every 15 minutes for
   near-immediate rewards, hourly for gentler) delivering the script's stdout.
   Completion criterion: the job appears in `cronjob(action='list')`.
6. **Add an evening `--streak-check` job.** The highest-value notification in
   the whole skill — it warns before a streak is lost rather than celebrating
   after the fact.

## Mechanics

- XP by Todoist API priority: **4 → 50, 3 → 30, 2 → 20, 1 → 10**. The API integer
  is inverted against the app's P1–P4 labels: API 4 is the urgent one.
- **Flat daily streak bonus**: the first completion of each day pays a fixed
  `10 XP × tier` (tier 1 under 10 days, 2 from 10, 3 from 30 — 30 XP max). Paid
  once per day, never per task.
- Levels: advancing **from** level L costs `round(100 * L**1.5)` XP, capped at
  level 50. Surplus rolls over. Reading this as a cumulative total makes levels
  arrive far too fast.
- Streak: one calendar day with ≥1 completion. Every 7 continuous days banks a
  **freeze** (max 2). A missed day consumes a freeze and preserves the streak;
  with none banked the streak resets to 0. `longest_streak` is kept separately.
- Achievements are derived from existing counters, never authored or stored.
  Base set: First Spark (1 task, +50), Getting Going (25, +30), Century Club
  (1000 XP, +200), Week One (7-day streak, +100), Unbroken (30-day, +300).
- **Per-category ladders are generated, not authored**: for every category the
  ledger has seen, `{category} Apprentice` (10), `Master` (20) and `Expert` (50)
  appear automatically at +50/+100/+200. A user working in three areas gets
  three ladders for free. Unclassified tasks file under `inbox` and get one too.
- **Achievement bonuses are paid once, on unlock, and shown on their own line.**
  Unlocking only ever happens in `check_achievements()`; `earned_achievements()`
  is a read-only view for `--status`, so rendering can never pay a bonus twice.
- **Highlight throttle**: at most `HIGHLIGHT_CAP` (2) emphasised messages per
  day, priority level-up > achievement > streak milestone. Past the cap the
  completion still reports, just without a shout. This is what keeps the
  celebrations worth reading on day 90.
- **Pause flag**: `"active": false` in the config stops everything silently —
  no awards, no output, no ledger writes.
- **Streak resets are never phrased as a loss.** `expire_streak()` returns
  "A new streak starts today." Punishment framing is what makes people abandon
  a streak system outright.
- **Near-unlock targets appear only in `--status`**, never in cron output.
- **Ingest contract**: `normalize_record()` reduces every backend record to
  exactly `{id, title, completed_at, priority, category, source}`. Anything
  malformed is dropped. No backend can widen the scoring surface.

## Pitfalls

- **Silence is correct.** `--poll` prints nothing when nothing completed. Anything
  printed on a no-op becomes a notification every single run.
- **Never add a "grant XP" command.** Rewards must only come from real
  completions, or the numbers stop meaning anything.
- **Markdown has no stable identity.** Task IDs hash the normalised task text, so
  moving a task between files keeps its identity, but editing its text starts a
  new task and re-earning its XP is not possible.
- **Undated markdown is stamped at detection time.** Only the Obsidian Tasks
  (`✅ YYYY-MM-DD`) and todo.txt (`x YYYY-MM-DD`) forms carry a real completion
  date. Say so rather than implying precision that isn't there.
- **Todoist free-tier history is truncated.** Poll at least daily; older
  completions become unreachable and their rewards are lost for good.
- **Duplicate task text** in one scan gets `#2`, `#3` suffixes. Genuinely
  identical repeated tasks are indistinguishable — acceptable, and documented.
- **XP spread invites gaming.** Marking chores urgent to farm levels will make the
  numbers meaningless. The 20:1 spread is deliberately narrower than it could be.
- **Never add stacking multipliers** — effort, due-date timing, or per-task
  streak. Each looks reasonable alone, but multiplied together they produce a
  ~38x spread between the worst and best single completion, trivialise early
  levels, and exhaust the whole ladder in weeks. A due-date multiplier also pays
  you to pad deadlines and punishes finishing at 12:05 AM. Consistency belongs
  in the flat daily bonus, which cannot be inflated by clearing trivial subtasks.
- **Don't reward effort you cannot verify.** Duration fields are set on a small
  minority of real tasks, so an effort multiplier would be dead code for almost
  everything while still adding a config surface.
- **Category tags are classification, not decoration.** The markdown backend
  reads the first real tag as the category and skips `#task`/`#todo`/`#done`
  (markers, not classes). A `#task`-only checklist therefore lands in `inbox` —
  correct, not a bug.
- **Never unlock achievements while rendering.** Unlocking pays XP; if a display
  path could unlock, every `--status` would mint free levels. Keep the split
  between `check_achievements()` (writes) and `earned_achievements()` (reads).
- **Credentials live in .env files, and there is more than one.** Profiles each
  have their own `.env`. Never hardcode a single path — that is how a skill
  works for its author and fails for everyone else. `find_token()` searches
  `$HERMES_HOME/.env`, then `~/.hermes/.env`, then every `profiles/*/.env`, and
  `--doctor` names the file it used. Never print the token, only its source.
- **A cron run has no shell profile.** Anything that only reads the exported
  environment works interactively and dies on schedule. Resolve credentials
  from the filesystem, not just `os.environ`.
- **Profiles are islands.** Installing this skill into the default profile does
  not make it visible to a named profile such as a health or finance bot — each
  has its own skills directory. Copy it into every profile that needs it, and
  let `--doctor` confirm visibility rather than assuming.
- **One writer per ledger.** If two profiles both `--poll` the same scope they
  share a ledger and race, even with the file lock. Let one profile own the
  polling and let others read `--status`.

## Verification

- `--status` prints a level, streak and achievement list — proving the ledger
  parses and the derivation runs.
- Poll twice with no task changes: the second run must print nothing. Proof that
  idempotency holds.
- In the ledger file, `processed` is non-empty and `total_xp` matches the sum of
  the rewards observed.
- After a real completion, the next `--poll` pays XP exactly once.
