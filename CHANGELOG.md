# Changelog

All notable changes to task-rewards are documented here.

## [Unreleased]

## 0.2.0 (pending release)

- Use each completion's local calendar date for streaks, daily bonuses, and deduplication.
- Require one streak freeze per missed day and reset freeze progress after a broken streak.
- Preserve deduplication for repeatedly returned file records without an unsafe global 500-key cutoff.
- Derive the dedupe-key pruning cutoff from the poll's `since` watermark instead of a fixed keep-days window, so a long-gap backlog poll can never evict and re-award completions inside the backend's overlap window.
- Add Todoist cursor pagination, server-side project filtering, and automatic fallback when Todoist is unavailable during unattended setup.
- Isolate Todoist credentials to the current process environment.
- Harden JSON ingestion, setup path checks, timezone validation, config validation, and corrupt-ledger recovery reporting.
- Make checklist auto-detection select the matched file instead of an entire broad directory.
- Add publication contract tests and a Python 3.9–3.13 Linux/macOS CI matrix.

## 0.1.0

- Initial deterministic XP, levels, streaks, achievements, Todoist, Markdown, and JSON backends.
