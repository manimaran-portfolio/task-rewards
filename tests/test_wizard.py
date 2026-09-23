import _pathsetup  # noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wizard

REPO_ROOT = Path(__file__).resolve().parent.parent
REWARDS_PY = REPO_ROOT / "scripts" / "rewards.py"


class SlugifyTests(unittest.TestCase):
    def test_lowercases_and_dashes_spaces(self):
        self.assertEqual(wizard.slugify("Health Stuff"), "health-stuff")

    def test_strips_emoji_and_punctuation(self):
        self.assertEqual(wizard.slugify("Health \U0001f4aa!!"), "health")

    def test_empty_input_falls_back_to_default(self):
        self.assertEqual(wizard.slugify(""), "default")
        self.assertEqual(wizard.slugify("!!!"), "default")


class DetectTimezoneTests(unittest.TestCase):
    def test_uses_tz_env_var_when_it_looks_like_a_zone(self):
        with mock.patch.dict(os.environ, {"TZ": "America/Toronto"}, clear=False):
            self.assertEqual(wizard.detect_timezone(), "America/Toronto")

    def test_falls_back_to_utc_when_nothing_usable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TZ", None)
            with mock.patch.object(wizard.Path, "is_file", return_value=False), \
                 mock.patch.object(wizard.Path, "is_symlink", return_value=False):
                self.assertEqual(wizard.detect_timezone(), "UTC")

    def test_rejects_an_unknown_explicit_timezone(self):
        self.assertFalse(wizard.valid_timezone("Mars/Olympus_Mons"))

    def test_accepts_utc(self):
        self.assertTrue(wizard.valid_timezone("UTC"))


class ScanForChecklistTests(unittest.TestCase):
    def test_finds_a_directory_with_a_real_checkbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "notes"
            vault.mkdir()
            (vault / "log.md").write_text("- [x] Done thing\n", encoding="utf-8")
            with mock.patch.object(wizard, "COMMON_CHECKLIST_ROOTS", (str(vault.parent),)):
                found = wizard._scan_for_checklist()
            self.assertEqual(found, vault / "log.md")

    def test_auto_scan_does_not_search_hermes_internal_files(self):
        self.assertNotIn("~/.hermes", wizard.COMMON_CHECKLIST_ROOTS)

    def test_prose_only_notes_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "notes"
            vault.mkdir()
            (vault / "diary.md").write_text("Today I went for a walk.\n", encoding="utf-8")
            with mock.patch.object(wizard, "COMMON_CHECKLIST_ROOTS", (str(vault.parent),)):
                found = wizard._scan_for_checklist()
            self.assertIsNone(found)

    def test_nonexistent_roots_are_skipped_without_error(self):
        with mock.patch.object(wizard, "COMMON_CHECKLIST_ROOTS", ("/no/such/directory",)):
            self.assertIsNone(wizard._scan_for_checklist())


class AutoDetectTests(unittest.TestCase):
    def test_prefers_todoist_when_a_token_is_reachable(self):
        args = mock.Mock(backend=None, project_id=None, scope=None)
        fake_projects = [{"id": "1", "name": "Inbox"}, {"id": "2", "name": "Health"}]
        with mock.patch("backends.todoist.find_token", return_value=("tok", None)), \
             mock.patch("backends.todoist.list_projects", return_value=fake_projects):
            wizard._auto_detect(args)
        self.assertEqual(args.backend, "todoist")
        self.assertEqual(args.project_id, "1")
        self.assertEqual(args.scope, "inbox")

    def test_invalid_todoist_token_falls_back_to_checklist(self):
        args = mock.Mock(backend=None, project_id=None, scope=None, path=None, yes=False)
        checklist = Path("/tmp/tasks.md")
        with mock.patch("backends.todoist.find_token", return_value=("bad-token", None)), \
             mock.patch("backends.todoist.list_projects", side_effect=RuntimeError("401")), \
             mock.patch.object(wizard, "_scan_for_checklist", return_value=checklist):
            wizard._auto_detect(args)
        self.assertEqual(args.backend, "markdown")
        self.assertEqual(args.path, str(checklist))
        self.assertEqual(args.scope, "tasksmd")

    def test_picks_first_project_when_no_inbox_exists(self):
        args = mock.Mock(backend=None, project_id=None, scope=None)
        fake_projects = [{"id": "9", "name": "Groceries"}, {"id": "2", "name": "Health"}]
        with mock.patch("backends.todoist.find_token", return_value=("tok", None)), \
             mock.patch("backends.todoist.list_projects", return_value=fake_projects):
            wizard._auto_detect(args)
        self.assertEqual(args.project_id, "9")

    def test_falls_back_to_markdown_scan_when_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "notes"
            vault.mkdir()
            (vault / "log.md").write_text("- [x] Done thing\n", encoding="utf-8")
            args = mock.Mock(backend=None, project_id=None, scope=None, path=None)
            with mock.patch("backends.todoist.find_token", return_value=(None, None)), \
                 mock.patch.object(wizard, "COMMON_CHECKLIST_ROOTS", (str(vault.parent),)):
                wizard._auto_detect(args)
            self.assertEqual(args.backend, "markdown")
            self.assertEqual(args.path, str(vault / "log.md"))

    def test_creates_a_blank_checklist_as_last_resort(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(backend=None, project_id=None, scope=None, path=None)
            with mock.patch("backends.todoist.find_token", return_value=(None, None)), \
                 mock.patch.object(wizard, "COMMON_CHECKLIST_ROOTS", ()), \
                 mock.patch.object(wizard.profile_paths, "default_checklist_path",
                                   return_value=Path(tmp) / "tasks.md"):
                wizard._auto_detect(args)
            self.assertEqual(args.backend, "markdown")
            created = Path(args.path)
            self.assertTrue(created.exists())
            self.assertIn("- [ ]", created.read_text(encoding="utf-8"))

    def test_explicit_backend_is_never_overridden(self):
        args = mock.Mock(backend="json", project_id=None, scope=None, path="/x.json")
        wizard._auto_detect(args)
        self.assertEqual(args.backend, "json")


class SetupAutoSubprocessTests(unittest.TestCase):
    """A couple of true end-to-end checks that --setup --auto works as a
    real, unattended `python3 scripts/rewards.py` invocation would be run
    right after the skill is installed."""

    def _run(self, home: Path, config_path: Path, extra_env=None):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "HERMES_HOME": str(home / ".hermes")}
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(REWARDS_PY), "--setup", "--auto", "--config", str(config_path)],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_zero_touch_install_with_nothing_available_still_activates(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_path = home / ".hermes" / "task-rewards.json"
            r = self._run(home, cfg_path, extra_env={})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertTrue(cfg["active"])
            self.assertEqual(cfg["backend"], "markdown")
            self.assertTrue(Path(cfg["backend_options"]["path"]).exists())

    def test_zero_touch_install_adopts_an_existing_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            vault = home / "Documents"
            vault.mkdir()
            (vault / "log.md").write_text("- [x] Existing task\n", encoding="utf-8")
            cfg_path = home / ".hermes" / "task-rewards.json"
            r = self._run(home, cfg_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(cfg["backend"], "markdown")
            self.assertEqual(cfg["backend_options"]["path"], str(vault / "log.md"))

    def test_json_setup_rejects_malformed_input_before_writing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / "bad.ndjson"
            source.write_text('{"id":"1"}\nnot json\n', encoding="utf-8")
            cfg_path = home / ".hermes" / "task-rewards.json"
            env = {"PATH": "/usr/bin:/bin", "HOME": str(home),
                   "HERMES_HOME": str(home / ".hermes")}
            r = subprocess.run([
                sys.executable, str(REWARDS_PY), "--setup", "--yes",
                "--backend", "json", "--path", str(source), "--scope", "test",
                "--timezone", "UTC", "--config", str(cfg_path),
            ], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertFalse(cfg_path.exists())
            self.assertIn("line 2", r.stdout)


if __name__ == "__main__":
    unittest.main()
