"""Setup wizard and environment doctor for task-rewards.

The scoring engine is the easy part. Every real-world failure came from the
*environment*: a credential that only existed in one profile's .env, a skill
installed into a profile that could not see it, a config written to a path the
cron job could not reach. So setup is a first-class command that discovers and
reports the environment instead of assuming it.

Two entry points:

    --doctor   report the environment and stop. Read-only, never writes.
    --setup    discover, confirm, write a config, and run a baseline dry-run.

Both work non-interactively (flags or sensible defaults) so an agent can drive
them, and interactively when a TTY is attached.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEZONE = "America/Toronto"
DEFAULT_XP = {"4": 50, "3": 30, "2": 20, "1": 10}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _tick(ok: bool) -> str:
    return "\u2713" if ok else "\u2717"


def _short(p) -> str:
    """Home-relative display path."""
    try:
        return str(p).replace(str(Path.home()), "~", 1)
    except Exception:
        return str(p)


def detect_timezone() -> str:
    """Best-effort IANA zone. Never invents a wrong one — falls back to UTC."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz
    etc = Path("/etc/timezone")
    if etc.is_file():
        try:
            val = etc.read_text(encoding="utf-8").strip()
            if val and "/" in val:
                return val
        except OSError:
            pass
    link = Path("/etc/localtime")
    if link.is_symlink():
        try:
            target = str(link.resolve())
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    return "UTC"


def slugify(name: str) -> str:
    """'Health \U0001f4aa' -> 'health'. Safe for a scope name and a filename."""
    ascii_only = re.sub(r"[^A-Za-z0-9 _-]", "", name)
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.strip().lower()).strip("-") or "default"


def _prompt(question: str, default: str | None = None, assume_yes: bool = False) -> str:
    """Ask, unless we are non-interactive or told to assume defaults."""
    if assume_yes or not sys.stdin.isatty():
        return default or ""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default or ""
    return answer or (default or "")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(cfg_path: Path) -> int:
    """Report the environment. Read-only."""
    here = Path(__file__).resolve().parent
    skill_dir = here.parent
    out: list[str] = ["task-rewards \u2014 environment check", ""]

    # runtime
    out.append("Runtime")
    out.append(f"  {_tick(sys.version_info >= (3, 9))} python        "
               f"{sys.version.split()[0]} (need 3.9+)")
    out.append(f"      skill dir     {_short(skill_dir)}")
    hh = os.environ.get("HERMES_HOME")
    if hh:
        profile = Path(hh).name
        if profile in (".hermes", ""):
            profile = "default"
        out.append(f"      HERMES_HOME   {_short(hh)}  (profile: {profile})")
    else:
        out.append("      HERMES_HOME   unset (running as the default profile)")

    # credentials
    out.append("")
    out.append("Credentials")
    try:
        sys.path.insert(0, str(here))
        from backends import todoist  # noqa: E402
        token, src = todoist.find_token()
        if token:
            where = _short(src) if src else "the environment"
            out.append(f"  {_tick(True)} TODOIST_API_TOKEN  found in {where} "
                       f"(len {len(token)})")
            try:
                projects = todoist.list_projects()
                out.append(f"  {_tick(True)} live API          "
                           f"{len(projects)} project(s) visible")
            except Exception as exc:  # noqa: BLE001
                out.append(f"  {_tick(False)} live API          "
                           f"{type(exc).__name__}: {str(exc)[:70]}")
        else:
            out.append(f"  {_tick(False)} TODOIST_API_TOKEN  not found")
            out.append("      searched:")
            for p in todoist.env_candidates():
                out.append(f"        - {_short(p)}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  {_tick(False)} backend import failed: {exc}")

    # config
    out.append("")
    out.append("Config")
    if not cfg_path.is_file():
        out.append(f"  {_tick(False)} {_short(cfg_path)}  \u2014 not found")
        out.append("      run --setup to create it")
    else:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            out.append(f"  {_tick(True)} {_short(cfg_path)}  \u2014 valid JSON")
            for key in ("scope", "backend", "timezone", "active"):
                if key in cfg:
                    out.append(f"      {key:<12}  {cfg[key]}")
            bo = cfg.get("backend_options") or {}
            if bo.get("project_id"):
                out.append(f"      project_id    {bo['project_id']}")
            if bo.get("path"):
                out.append(f"      path          {_short(bo['path'])}")

            # ledger
            out.append("")
            out.append("Ledger")
            lp = Path(os.path.expanduser(cfg.get("ledger", "")))
            if not str(lp):
                out.append(f"  {_tick(False)} no ledger key in config")
            else:
                parent_ok = lp.parent.is_dir() or os.access(
                    lp.parent if lp.parent.exists() else lp.parent.parent, os.W_OK)
                exists = lp.is_file()
                out.append(f"  {_tick(parent_ok)} {_short(lp)}  "
                           f"\u2014 {'exists' if exists else 'will be created'}")
                if exists:
                    try:
                        state = json.loads(lp.read_text(encoding="utf-8"))
                        out.append(f"      level {state.get('level', 1)}  "
                                   f"xp {state.get('total_xp', 0)}  "
                                   f"streak {state.get('streak_days', 0)}d  "
                                   f"tasks {state.get('tasks_completed', 0)}")
                    except (OSError, ValueError):
                        out.append(f"  {_tick(False)} ledger exists but is not "
                                   "valid JSON")
        except (OSError, ValueError) as exc:
            out.append(f"  {_tick(False)} config unreadable: {exc}")

    # skills visibility — the multi-profile trap
    out.append("")
    out.append("Visibility")
    hh_skills = (Path(hh) / "skills") if hh else (Path.home() / ".hermes" / "skills")
    mine = skill_dir.resolve()
    try:
        inside = hh_skills.resolve() in mine.parents
    except OSError:
        inside = False
    out.append(f"  {_tick(inside)} installed under this profile's skills dir")
    if not inside:
        out.append(f"      this copy lives at {_short(mine)}")
        out.append(f"      but the active profile reads {_short(hh_skills)}")
        out.append("      the agent will NOT discover the skill from here")

    print("\n".join(out))
    return 0


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def _backend_choices() -> dict:
    """What task sources look usable right now."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from backends import todoist  # noqa: E402
    token, src = todoist.find_token()
    return {"todoist": (token, src)}


def cmd_setup(args) -> int:
    """Discover, confirm, write, and dry-run. Returns a shell exit code."""
    assume_yes = bool(getattr(args, "yes", False))
    cfg_path = Path(os.path.expanduser(args.config))
    force = bool(getattr(args, "force", False))

    print("task-rewards \u2014 setup")
    print("=" * 40)
    print()

    if cfg_path.is_file() and not force:
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        print(f"A config already exists at {_short(cfg_path)}")
        print(f"  scope: {existing.get('scope', '?')}   "
              f"backend: {existing.get('backend', '?')}")
        print()
        print("Re-run with --force to overwrite, or use --status to view it.")
        return 0

    # ---- 1. runtime -------------------------------------------------------
    print("1. Runtime")
    if sys.version_info < (3, 9):
        print(f"   {_tick(False)} python {sys.version.split()[0]} is too old "
              "(need 3.9+)")
        return 2
    print(f"   {_tick(True)} python {sys.version.split()[0]}")
    hh = os.environ.get("HERMES_HOME")
    if hh:
        prof = Path(hh).name
        prof = "default" if prof in (".hermes", "") else prof
    else:
        prof = "default"
    print(f"   {_tick(True)} profile: {prof}")
    print()

    # ---- 2. task source ---------------------------------------------------
    print("2. Task source")
    choices = _backend_choices()
    token, src = choices["todoist"]

    backend = getattr(args, "backend", None)
    if not backend:
        if token:
            backend = "todoist"
        else:
            backend = "markdown"
            print("   no Todoist token found, falling back to markdown")

    backend_options: dict = {}
    project_name = ""
    if backend == "todoist":
        if not token:
            print(f"   {_tick(False)} TODOIST_API_TOKEN not found. Add it to one of:")
            here = Path(__file__).resolve().parent
            sys.path.insert(0, str(here))
            from backends import todoist  # noqa: E402
            for p in todoist.env_candidates():
                print(f"       - {_short(p)}")
            print("   Then re-run --setup.")
            return 2
        where = _short(src) if src else "the environment"
        print(f"   {_tick(True)} Todoist token found in {where}")

        try:
            from backends import todoist as _td  # noqa: E402
            projects = _td.list_projects()
        except Exception as exc:  # noqa: BLE001
            print(f"   {_tick(False)} could not list projects: "
                  f"{type(exc).__name__}: {str(exc)[:70]}")
            return 2
        print(f"   {_tick(True)} live API reachable \u2014 {len(projects)} project(s)")
        print()

        project_id = getattr(args, "project_id", None)
        if not project_id:
            if sys.stdin.isatty() and not assume_yes:
                print("   Which project should this ledger track?")
                for i, p in enumerate(projects, 1):
                    print(f"     {i:>2}. {p.get('name')}")
                choice = _prompt("   number or name", assume_yes=assume_yes)
                for p in projects:
                    if choice and (choice.lower() == str(p.get("name", "")).lower()
                                   or choice == str(p.get("id"))):
                        project_id = p.get("id")
                        project_name = p.get("name", "")
                        break
                if not project_id and choice.isdigit():
                    idx = int(choice) - 1
                    if 0 <= idx < len(projects):
                        project_id = projects[idx].get("id")
                        project_name = projects[idx].get("name", "")
            if not project_id:
                print("   No project chosen. Pass --project-id <id>, or run:")
                print("     --setup --list-projects")
                return 2
        else:
            for p in projects:
                if str(p.get("id")) == str(project_id):
                    project_name = p.get("name", "")
            if not project_name:
                print(f"   {_tick(False)} project id {project_id} not in your account")
                return 2

        backend_options["project_id"] = project_id
        if project_name:
            print(f"   selected: {project_name}  ({project_id})")

    else:  # markdown
        target = getattr(args, "path", None)
        if not target:
            target = _prompt("   path to a checklist file or folder",
                             assume_yes=assume_yes)
        if not target:
            print("   markdown needs --path <file-or-folder>")
            return 2
        target = os.path.expanduser(target)
        p = Path(target)
        if not p.exists():
            print(f"   {_tick(False)} path does not exist: {_short(p)}")
            return 2
        kind = "directory" if p.is_dir() else "file"
        print(f"   {_tick(True)} path exists ({kind})")
        if p.is_dir():
            n = len(list(p.rglob("*.md")))
            print(f"      {n} markdown file(s) under it")
            if n == 0:
                print("   no .md files found there \u2014 check the path")
                return 2
        backend_options["path"] = target

    print()

    # ---- 3. scope, category, timezone ------------------------------------
    print("3. Scope")
    scope = getattr(args, "scope", None)
    if not scope:
        default_scope = slugify(project_name) if backend == "todoist" and project_name \
            else slugify(Path(backend_options.get("path", "tasks")).stem)
        scope = _prompt("   short name for this ledger", default_scope,
                        assume_yes=assume_yes) or default_scope
    scope = slugify(scope)
    print(f"   {_tick(True)} scope: {scope}")

    category = getattr(args, "category", None) or scope
    backend_options.setdefault("category", category)

    tzname = getattr(args, "timezone", None)
    if not tzname:
        detected = detect_timezone()
        tzname = _prompt("   timezone (IANA)", detected, assume_yes=assume_yes) or detected
    print(f"   {_tick(True)} timezone: {tzname}")
    print()

    # ---- 4. write ---------------------------------------------------------
    ledger = str(getattr(args, "ledger_file", None) or
                 Path.home() / ".hermes" / f"task-rewards-{scope}-ledger.json")
    cfg = {
        "scope": scope,
        "active": True,
        "backend": backend,
        "backend_options": backend_options,
        "ledger": ledger,
        "timezone": tzname,
        "notify": getattr(args, "notify", None) or "digest",
        "xp": DEFAULT_XP,
    }
    print("4. Writing config")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, cfg_path)
    print(f"   {_tick(True)} {_short(cfg_path)}")
    print(f"      ledger  {_short(ledger)}")
    print(f"      xp      4->50  3->30  2->20  1->10")
    print()

    # ---- 5. baseline dry-run ---------------------------------------------
    print("5. Baseline dry-run (awards nothing)")
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import importlib  # noqa: E402
    rewards = importlib.import_module("rewards")
    try:
        state = rewards.load_json(Path(os.path.expanduser(ledger)),
                                  rewards.default_state(scope))
        out = rewards.cmd_poll(cfg, state, Path(os.path.expanduser(ledger)), quiet=False)
        print(f"   {_tick(True)} {out or 'baseline set'}")
    except Exception as exc:  # noqa: BLE001
        print(f"   {_tick(False)} {type(exc).__name__}: {str(exc)[:100]}")
        print("   config was written; fix the error and re-run --poll")
        return 2
    print()

    # ---- 6. next steps ----------------------------------------------------
    # Point at the CLI entry point, not this module — wizard.py has no main().
    script = Path(__file__).resolve().parent / "rewards.py"
    print("6. Next steps")
    print(f"   status : python3 {_short(script)} --config {_short(cfg_path)} --status")
    print(f"   poll   : python3 {_short(script)} --config {_short(cfg_path)} --poll")
    print(f"   doctor : python3 {_short(script)} --config {_short(cfg_path)} --doctor")
    print()
    print("   To earn rewards automatically, schedule --poll (every 15 min) and")
    print("   --streak-check (evening). Ask Hermes to create those cron jobs \u2014")
    print("   the skill will confirm before touching your scheduler.")
    if backend == "todoist":
        print()
        print("   Note: Todoist's free tier truncates completion history. Poll at")
        print("   least daily or older completions age out unrewarded.")
    return 0