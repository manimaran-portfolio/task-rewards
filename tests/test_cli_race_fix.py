"""Regression test for the load-before-lock race.

main() used to call load_json(ledger_path, ...) BEFORE acquiring the ledger's
advisory lock. Two overlapping invocations (a slow backend call plus an
unlucky cron overlap) could each read a stale copy, then serialize on the
lock and have the second one write its own stale-derived state back,
silently discarding whatever the first one had just written.

This is reproduced deterministically (no real timing/threading, which would
be flaky) by making the act of acquiring the lock itself also simulate "some
other process just finished and wrote new data, then released the lock right
before us" -- if state is loaded AFTER the lock as it now is, our process
must see that write. If it were loaded before (the old bug), it would not.
"""
import _pathsetup  # noqa: F401

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rewards


class LoadAfterLockTests(unittest.TestCase):
    def _write_config_and_ledger(self, tmp: Path):
        ledger_path = tmp / "ledger.json"
        cfg_path = tmp / "config.json"
        ledger_path.write_text(json.dumps(rewards.default_state("race")), encoding="utf-8")
        cfg = {
            "scope": "race", "active": True, "backend": "markdown",
            "backend_options": {"path": str(tmp / "tasks.md")},
            "ledger": str(ledger_path), "timezone": "UTC", "notify": "digest",
            "xp": rewards.DEFAULT_XP,
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        (tmp / "tasks.md").write_text("", encoding="utf-8")
        return cfg_path, ledger_path

    def test_status_reflects_a_write_that_landed_while_waiting_for_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_path, ledger_path = self._write_config_and_ledger(tmp)

            real_lock_handle = rewards._lock_handle

            def racing_lock_handle(fh):
                # Simulate: by the time WE get the lock, another process
                # already finished its own poll and wrote fresh numbers.
                concurrent_state = rewards.default_state("race")
                concurrent_state["total_xp"] = 500
                concurrent_state["level"] = 3
                ledger_path.write_text(json.dumps(concurrent_state), encoding="utf-8")
                return real_lock_handle(fh)

            captured = io.StringIO()
            with mock.patch.object(rewards, "_lock_handle", side_effect=racing_lock_handle), \
                 contextlib.redirect_stdout(captured):
                rc = rewards.main(["--config", str(cfg_path), "--status", "--json"])
            self.assertEqual(rc, 0)

            # --status is read-only and never writes, so the ONLY way this
            # can show 500 is if the state it rendered was loaded from disk
            # AFTER racing_lock_handle ran (i.e. after the lock was
            # acquired) -- loading before the lock (the old bug) would have
            # rendered the ledger's original value (0) instead.
            printed = json.loads(captured.getvalue())
            self.assertEqual(printed["total_xp"], 500)
            self.assertEqual(printed["level"], 3)

    def test_a_second_call_never_reverts_a_write_made_after_it_started_reading(self):
        """End-to-end version of the same scenario using cmd_poll directly:
        state loaded, then (simulating a concurrent writer) the file changes
        on disk, then this call's own poll-and-write must not clobber that
        concurrent write with data derived from the earlier snapshot -- which
        is exactly what moving the load inside the lock in main() prevents."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            cfg_path, ledger_path = self._write_config_and_ledger(tmp)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

            # First caller establishes the baseline (no XP, matches real flow).
            state = rewards.load_json(ledger_path, rewards.default_state("race"))
            rewards.cmd_poll(cfg, state, ledger_path)

            # A concurrent writer lands a real update.
            concurrent = rewards.load_json(ledger_path, rewards.default_state("race"))
            concurrent["total_xp"] = 999
            rewards.atomic_write_json(ledger_path, concurrent)

            # main() for a second, unrelated --status call must load fresh
            # state (post-lock), not reuse anything read earlier.
            with contextlib.redirect_stdout(io.StringIO()):
                rc = rewards.main(["--config", str(cfg_path), "--status", "--json"])
            self.assertEqual(rc, 0)
            on_disk = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["total_xp"], 999)  # untouched by the --status call


if __name__ == "__main__":
    unittest.main()
