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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_paths  # noqa: E402

DEFAULT_TIMEZONE = "America/Toronto"
DEFAULT_XP = {"4": 50, "3": 30, "2": 20, "1": 10}

# Common places a checklist might already live, for --setup --auto. Only a
# directory that actually contains a checkbox/todo.txt line we understand
# gets adopted — a folder of prose notes must never become a "task list".
COMMON_CHECKLIST_ROOTS = (
    "~/Documents", "~/Obsidian", "~/obsidian", "~/notes", "~/Notes",
    "~/vault", "~/Vault",
)
MAX_AUTO_SCAN_FILES = 300


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
    if tz and valid_timezone(tz):
        return tz
    etc = Path("/etc/timezone")
    if etc.is_file():
        try:
            val = etc.read_text(encoding="utf-8").strip()
            if val and valid_timezone(val):
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


def valid_timezone(name: str) -> bool:
    """Whether an IANA timezone can be loaded on this Python installation."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return True


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
            out.append("      configure it through Hermes' secure skill setup")
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
            tzname = cfg.get("timezone", "UTC")
            out.append(f"  {_tick(valid_timezone(tzname))} timezone data   {tzname}")
            bo = cfg.get("backend_options") or {}
            if bo.get("project_id"):
                out.append(f"      project_id    {bo['project_id']}")
            if bo.get("path"):
                out.append(f"      path          {_short(bo['path'])}")
                source_path = Path(os.path.expanduser(str(bo["path"])))
                if cfg.get("backend") == "json":
                    out.append(f"  {_tick(source_path.is_file())} JSON source is a regular file")
                else:
                    out.append(f"  {_tick(source_path.exists())} source path exists")

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

def _scan_for_checklist() -> Path | None:
    """First existing common location that already contains a real checklist.

    Caps how much it reads (MAX_AUTO_SCAN_FILES) so an install-time scan of a
    huge notes vault can't hang; that means it can miss one buried deep, but
    a fast, mostly-right auto-detect beats a slow, exhaustive one here — the
    user can always point --path at the right folder by hand afterwards.
    """
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from backends.markdown import MD_CHECKBOX, TODOTXT  # noqa: E402
    scanned = 0
    for root in COMMON_CHECKLIST_ROOTS:
        p = Path(root).expanduser()
        if not p.is_dir():
            continue
        for md in sorted(p.rglob("*.md")):
            if scanned >= MAX_AUTO_SCAN_FILES:
                return None
            scanned += 1
            try:
                lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if any(MD_CHECKBOX.match(line) or TODOTXT.match(line) for line in lines):
                return md
    return None


def _auto_detect(args) -> None:
    """Fill in backend/project_id/path/scope for `--setup --auto`.

    Priority, matching the skill's own doc for how it should install itself:
    Todoist (if a token is already reachable) > an existing checklist found
    in a common location > a fresh blank checklist. Never prompts — this is
    meant to run unattended right after the skill is installed, so it always
    ends with something active rather than stuck waiting on the user.
    """
    args.yes = True
    if getattr(args, "backend", None):
        return  # caller (or a previous --setup flag) already pinned one

    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from backends import todoist  # noqa: E402
    token, _src = todoist.find_token()
    if token:
        try:
            projects = todoist.list_projects()
        except Exception:  # an unusable token must not block the safe fallback
            projects = None
        if projects is not None:
            if not getattr(args, "project_id", None):
                pick = next((p for p in projects
                            if str(p.get("name", "")).strip().lower() == "inbox"), None)
                pick = pick or (projects[0] if projects else None)
                if pick:
                    args.project_id = pick.get("id")
                    if not getattr(args, "scope", None):
                        args.scope = slugify(pick.get("name") or "tasks")
            if getattr(args, "project_id", None):
                args.backend = "todoist"
                return

    found = _scan_for_checklist()
    if found:
        args.backend = "markdown"
        args.path = str(found)
        if not getattr(args, "scope", None):
            args.scope = slugify(found.name)
        return

    # Nothing to adopt: create a fresh plain checklist rather than leaving
    # the install with no active source at all.
    blank = profile_paths.default_checklist_path()
    if not blank.exists():
        blank.parent.mkdir(parents=True, exist_ok=True)
        blank.write_text(
            "# Tasks\n\n"
            "Check items off as you complete them — task-rewards polls this file.\n\n"
            "- [ ] Add your first task here\n",
            encoding="utf-8",
        )
    args.backend = "markdown"
    args.path = str(blank)
    if not getattr(args, "scope", None):
        args.scope = "tasks"


def _backend_choices() -> dict:
    """What task sources look usable right now."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from backends import todoist  # noqa: E402
    token, src = todoist.find_token()
    return {"todoist": (token, src)}


def cmd_setup(args) -> int:
    """Discover, confirm, write, and dry-run. Returns a shell exit code."""
    cfg_path = Path(os.path.expanduser(args.config))
    force = bool(getattr(args, "force", False))

    print("task-rewards \u2014 setup")
    print("=" * 40)
    print()

    if getattr(args, "auto", False):
        _auto_detect(args)
        print(f"0. Auto-detect: backend={args.backend}"
              + (f"  project_id={args.project_id}" if getattr(args, "project_id", None) else "")
              + (f"  path={_short(args.path)}" if getattr(args, "path", None) else ""))
        print()
    assume_yes = bool(getattr(args, "yes", False))

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
            print(f"   {_tick(False)} TODOIST_API_TOKEN not found.")
            print("   Configure it through Hermes' secure skill setup, then re-run --setup.")
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

    elif backend in {"markdown", "json"}:
        target = getattr(args, "path", None)
        if not target:
            target = _prompt("   path to a checklist file or folder",
                             assume_yes=assume_yes)
        if not target:
            print(f"   {backend} needs --path <file-or-folder>")
            return 2
        target = os.path.expanduser(target)
        p = Path(target)
        if not p.exists():
            print(f"   {_tick(False)} path does not exist: {_short(p)}")
            return 2
        kind = "directory" if p.is_dir() else "file"
        print(f"   {_tick(True)} path exists ({kind})")
        if backend == "json":
            from backends import json as _json_backend  # noqa: E402
            error = _json_backend.validate_file(p)
            if error:
                print(f"   {_tick(False)} {error}")
                return 2
        elif p.is_dir():
            n = len(list(p.rglob("*.md")))
            print(f"      {n} markdown file(s) under it")
            if n == 0:
                print("   no .md files found there \u2014 check the path")
                return 2
        backend_options["path"] = target
    else:
        print(f"   {_tick(False)} unsupported backend: {backend}")
        return 2

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

    # Only the Todoist backend actually reads backend_options["category"] — it
    # has no other way to classify a completion. markdown/json backends derive
    # a category from each task's own tag/folder/field, so writing one here
    # for them would be a config value that looks meaningful but is silently
    # ignored.
    if backend == "todoist":
        category = getattr(args, "category", None) or scope
        backend_options.setdefault("category", category)

    tzname = getattr(args, "timezone", None)
    if not tzname:
        detected = detect_timezone()
        tzname = _prompt("   timezone (IANA)", detected, assume_yes=assume_yes) or detected
    if not valid_timezone(tzname):
        print(f"   {_tick(False)} unknown timezone: {tzname}")
        return 2
    print(f"   {_tick(True)} timezone: {tzname}")
    print()

    # ---- 4. write ---------------------------------------------------------
    ledger = str(getattr(args, "ledger_file", None) or profile_paths.default_ledger_path(scope))
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
        result = rewards.cmd_poll(cfg, state, Path(os.path.expanduser(ledger)))
        msg = (f"Baseline set — {result['baseline_count']} existing completion(s) recorded, "
              "no XP awarded. Rewards start from now.") if result.get("baseline") else "baseline set"
        print(f"   {_tick(True)} {msg}")
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