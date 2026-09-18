"""End-to-end tests that invoke scripts/rewards.py as a real subprocess,
the same way a cron job or a Hermes agent would."""
import _pathsetup  # noqa: F401

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REWARDS_PY = REPO_ROOT / "scripts" / "rewards.py"


def run(*args, env_extra=None):
    env = {"PATH": "/usr/bin:/bin"}
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, str(REWARDS_PY), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return result


class MarkdownEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_ctx.name)
        self.tasks_md = self.tmp / "tasks.md"
        self.tasks_md.write_text("", encoding="utf-8")
        self.ledger = self.tmp / "ledger.json"
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({
            "scope": "e2e", "active": True, "backend": "markdown",
            "backend_options": {"path": str(self.tasks_md)},
            "ledger": str(self.ledger), "timezone": "UTC", "notify": "digest",
            "xp": {"4": 50, "3": 30, "2": 20, "1": 10},
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp_ctx.cleanup()

    def test_first_poll_is_a_zero_xp_baseline(self):
        self.tasks_md.write_text("- [x] #task Old task ✅ 2026-09-01\n", encoding="utf-8")
        r = run("--config", str(self.cfg_path), "--poll")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Baseline set", r.stdout)
        status = run("--config", str(self.cfg_path), "--status", "--json")
        self.assertEqual(json.loads(status.stdout)["total_xp"], 0)

    def test_new_completion_after_baseline_is_rewarded_exactly_once(self):
        run("--config", str(self.cfg_path), "--poll")  # baseline
        self.tasks_md.write_text("- [x] #task Do a thing ✅ 2026-09-18\n", encoding="utf-8")

        first = run("--config", str(self.cfg_path), "--poll")
        self.assertIn("XP", first.stdout)

        second = run("--config", str(self.cfg_path), "--poll")
        self.assertEqual(second.stdout.strip(), "")  # silent: nothing new

        status = json.loads(run("--config", str(self.cfg_path), "--status", "--json").stdout)
        self.assertEqual(status["tasks_completed"], 1)

    def test_paused_config_produces_no_output_and_no_award(self):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["active"] = False
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.tasks_md.write_text("- [x] #task Do a thing ✅ 2026-09-18\n", encoding="utf-8")

        r = run("--config", str(self.cfg_path), "--poll")
        self.assertEqual(r.stdout.strip(), "")
        self.assertFalse(self.ledger.exists())  # not even a baseline was recorded

    def test_poll_then_status_combo_shows_up_to_date_numbers_in_one_call(self):
        run("--config", str(self.cfg_path), "--poll")  # baseline
        self.tasks_md.write_text("- [x] #task Do a thing ✅ 2026-09-18\n", encoding="utf-8")
        combo = json.loads(run("--config", str(self.cfg_path), "--poll", "--status", "--json").stdout)
        self.assertIn("poll", combo)
        self.assertIn("status", combo)
        self.assertEqual(combo["status"]["tasks_completed"], 1)
        self.assertEqual(combo["status"]["total_xp"], combo["poll"]["gained"])

    def test_ledger_dump_never_leaks_transient_scratch_keys(self):
        run("--config", str(self.cfg_path), "--poll")
        self.tasks_md.write_text("- [x] #task Do a thing ✅ 2026-09-18\n", encoding="utf-8")
        run("--config", str(self.cfg_path), "--poll")
        dumped = json.loads(run("--config", str(self.cfg_path), "--ledger").stdout)
        self.assertFalse(any(k.startswith("_") for k in dumped))

    def test_missing_config_reports_a_helpful_error_not_a_traceback(self):
        r = run("--config", str(self.tmp / "nope.json"), "--status")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--setup", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_no_flags_prints_help_instead_of_silently_doing_nothing(self):
        r = run("--config", str(self.cfg_path))
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage", r.stdout.lower())


class DedupeCapEvictionTests(unittest.TestCase):
    """SEEN_CAP eviction must drop the OLDEST processed keys, not an
    arbitrary subset -- regression test for the set-ordering bug (capping
    straight off `set(...)` has no reliable order, so it could evict a key
    from *this* poll and re-admit an ancient one instead). Done in-process
    so the real dedupe keys can be recomputed and checked precisely."""

    def test_eviction_drops_oldest_keys_first_not_an_arbitrary_subset(self):
        import rewards as rewards_mod
        from backends import markdown as markdown_backend

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            tasks_md = tmp / "tasks.md"
            ledger_path = tmp / "ledger.json"
            tasks_md.write_text("", encoding="utf-8")
            cfg = {
                "scope": "cap", "active": True, "backend": "markdown",
                "backend_options": {"path": str(tasks_md)},
                "ledger": str(ledger_path), "timezone": "UTC", "notify": "digest",
                "xp": rewards_mod.DEFAULT_XP,
            }
            state = rewards_mod.default_state("cap")
            rewards_mod.cmd_poll(cfg, state, ledger_path)  # baseline, empty

            # Push well past SEEN_CAP (500) distinct completions in one poll
            # so eviction actually has to kick in. Left undated so they're
            # stamped "now" and clear the watermark set by the baseline.
            n = rewards_mod.SEEN_CAP + 50
            lines = [f"- [x] Task {i:04d}\n" for i in range(n)]
            tasks_md.write_text("".join(lines), encoding="utf-8")
            state = rewards_mod.load_json(ledger_path, rewards_mod.default_state("cap"))
            rewards_mod.cmd_poll(cfg, state, ledger_path)

            state = rewards_mod.load_json(ledger_path, rewards_mod.default_state("cap"))
            self.assertEqual(len(state["processed"]), rewards_mod.SEEN_CAP)
            self.assertEqual(state["tasks_completed"], n)

            # Recompute the REAL dedupe keys the backend actually assigned,
            # for the very first and very last task in scan order.
            recs = markdown_backend.list_completions("", cfg)
            by_title = {r["title"]: r for r in recs}

            def key_of(title):
                return rewards_mod.dedupe_key(rewards_mod.normalize_record(by_title[title]))

            key_first, key_last = key_of("Task 0000"), key_of(f"Task {n - 1:04d}")
            self.assertNotIn(key_first, state["processed"])  # oldest: evicted
            self.assertIn(key_last, state["processed"])      # newest: retained

            # Re-polling: exactly the 50 evicted (oldest) tasks look new
            # again -- confirms the eviction boundary end-to-end, not just
            # in the processed list.
            rewards_mod.cmd_poll(cfg, state, ledger_path)
            state2 = rewards_mod.load_json(ledger_path, rewards_mod.default_state("cap"))
            self.assertEqual(state2["tasks_completed"], n + 50)


if __name__ == "__main__":
    unittest.main()
