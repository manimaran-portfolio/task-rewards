"""Profile-aware path helpers shared by rewards.py, wizard.py and backends.

A Hermes profile is a directory holding its own skills/, .env, and (for this
skill) its own task-rewards config + ledger. HERMES_HOME names the active
profile's home. Every default path in this skill goes through here so that
installing it into a non-default profile "just works" without extra flags —
the same reasoning that already applied to the Todoist token lookup.
"""
from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    """The active profile's home directory. ~/.hermes when unset."""
    home = os.environ.get("HERMES_HOME")
    return Path(home).expanduser() if home else Path.home() / ".hermes"


def default_config_path() -> Path:
    return hermes_home() / "task-rewards.json"


def default_ledger_path(scope: str) -> Path:
    return hermes_home() / f"task-rewards-{scope}-ledger.json"


def default_checklist_path() -> Path:
    """Where --setup --auto creates a plain-text checklist if nothing else is found."""
    return hermes_home() / "tasks.md"


def discover_configs() -> list[Path]:
    """Existing task-rewards configs under this profile, for a helpful message.

    Also looks under ~/.hermes even when HERMES_HOME points elsewhere, since a
    config created before a profile switch should still be found.
    """
    roots = {hermes_home(), Path.home() / ".hermes"}
    found: list[Path] = []
    for root in roots:
        for pat in ("task-rewards*.json", "profiles/*/task-rewards*.json"):
            found.extend(root.glob(pat))
    uniq = sorted({p for p in found if not p.name.endswith("-ledger.json")}, key=str)
    return uniq
