import _pathsetup  # noqa: F401  must run before importing rewards

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import rewards


def base_state(scope="test"):
    return rewards.default_state(scope)


class NormalizeRecordTests(unittest.TestCase):
    def test_valid_record_passes_through(self):
        rec = rewards.normalize_record({
            "id": 42, "title": "Ship it", "completed_at": "2026-09-18T10:00:00Z",
            "priority": 3, "category": "Health Stuff", "source": "todoist",
        })
        self.assertEqual(rec["id"], "42")
        self.assertEqual(rec["priority"], 3)
        self.assertEqual(rec["category"], "health_stuff")  # lowered, spaces->underscore

    def test_missing_id_is_dropped(self):
        self.assertIsNone(rewards.normalize_record({"completed_at": "2026-09-18T10:00:00Z"}))

    def test_missing_completed_at_is_dropped(self):
        self.assertIsNone(rewards.normalize_record({"id": "1"}))

    def test_bad_timestamp_is_dropped(self):
        self.assertIsNone(rewards.normalize_record({"id": "1", "completed_at": "not-a-date"}))

    def test_non_dict_is_dropped(self):
        self.assertIsNone(rewards.normalize_record("not a dict"))
        self.assertIsNone(rewards.normalize_record(None))

    def test_bad_priority_becomes_none_not_a_crash(self):
        rec = rewards.normalize_record({"id": "1", "completed_at": "2026-09-18T10:00:00Z",
                                        "priority": "urgent"})
        self.assertIsNone(rec["priority"])

    def test_empty_category_falls_back_to_inbox(self):
        rec = rewards.normalize_record({"id": "1", "completed_at": "2026-09-18T10:00:00Z"})
        self.assertEqual(rec["category"], rewards.DEFAULT_CATEGORY)

    def test_title_is_truncated_and_defaulted(self):
        rec = rewards.normalize_record({"id": "1", "completed_at": "2026-09-18T10:00:00Z",
                                        "title": "x" * 200})
        self.assertEqual(len(rec["title"]), 120)
        rec2 = rewards.normalize_record({"id": "1", "completed_at": "2026-09-18T10:00:00Z",
                                         "title": "   "})
        self.assertEqual(rec2["title"], "(untitled)")


class DedupeKeyTests(unittest.TestCase):
    def test_same_day_same_id_same_key(self):
        r1 = {"source": "todoist", "id": "1", "completed_at": "2026-09-18T08:00:00Z"}
        r2 = {"source": "todoist", "id": "1", "completed_at": "2026-09-18T23:59:00Z"}
        self.assertEqual(rewards.dedupe_key(r1), rewards.dedupe_key(r2))

    def test_different_day_different_key(self):
        r1 = {"source": "todoist", "id": "1", "completed_at": "2026-09-18T08:00:00Z"}
        r2 = {"source": "todoist", "id": "1", "completed_at": "2026-09-19T08:00:00Z"}
        self.assertNotEqual(rewards.dedupe_key(r1), rewards.dedupe_key(r2))

    def test_different_source_different_key(self):
        r1 = {"source": "todoist", "id": "1", "completed_at": "2026-09-18T08:00:00Z"}
        r2 = {"source": "markdown", "id": "1", "completed_at": "2026-09-18T08:00:00Z"}
        self.assertNotEqual(rewards.dedupe_key(r1), rewards.dedupe_key(r2))

    def test_uses_user_calendar_date_not_utc_date(self):
        r1 = {"source": "todoist", "id": "1", "completed_at": "2026-09-19T00:30:00Z"}
        r2 = {"source": "todoist", "id": "1", "completed_at": "2026-09-18T22:00:00Z"}
        self.assertEqual(
            rewards.dedupe_key(r1, "America/Toronto"),
            rewards.dedupe_key(r2, "America/Toronto"),
        )


class ScoreAndLevelTests(unittest.TestCase):
    def test_score_uses_priority_table(self):
        table = rewards.DEFAULT_XP
        self.assertEqual(rewards.score({"priority": 4}, table), 50)
        self.assertEqual(rewards.score({"priority": 1}, table), 10)

    def test_score_defaults_missing_priority_to_lowest(self):
        self.assertEqual(rewards.score({"priority": None}, rewards.DEFAULT_XP), 10)

    def test_xp_to_next_grows_and_caps(self):
        self.assertGreater(rewards.xp_to_next(10), rewards.xp_to_next(1))
        self.assertEqual(rewards.xp_to_next(rewards.MAX_LEVEL), 0)

    def test_level_advance_stops_at_cap_and_zeroes_surplus(self):
        state = base_state()
        state["level"] = rewards.MAX_LEVEL - 1
        state["xp_into_level"] = 10 ** 9  # absurd surplus
        rewards._advance_levels(state)
        self.assertEqual(state["level"], rewards.MAX_LEVEL)
        self.assertEqual(state["xp_into_level"], 0)


class ApplyCompletionTests(unittest.TestCase):
    def test_first_completion_starts_streak_at_one(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        self.assertEqual(state["streak_days"], 1)
        self.assertEqual(state["tasks_completed"], 1)

    def test_consecutive_days_extend_streak(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        rewards.apply_completion(state, 10, date(2026, 9, 19))
        self.assertEqual(state["streak_days"], 2)

    def test_same_day_multiple_completions_do_not_double_streak(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        self.assertEqual(state["streak_days"], 1)
        self.assertEqual(state["tasks_completed"], 2)

    def test_daily_bonus_paid_once_per_day_not_per_task(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        first_bonus = state["_bonus"]
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        second_bonus = state["_bonus"]
        self.assertGreater(first_bonus, 0)
        self.assertEqual(second_bonus, 0)

    def test_gap_with_freeze_consumes_it_and_preserves_streak(self):
        state = base_state()
        for d in range(18, 18 + rewards.FREEZE_EVERY):
            rewards.apply_completion(state, 10, date(2026, 9, d))
        self.assertGreaterEqual(state["freezes"], 1)
        streak_before = state["streak_days"]
        freezes_before = state["freezes"]
        # skip a day, then complete something two days later
        rewards.apply_completion(state, 10, date(2026, 9, 18 + rewards.FREEZE_EVERY + 1))
        self.assertEqual(state["freezes"], freezes_before - 1)
        self.assertEqual(state["streak_days"], streak_before + 1)

    def test_gap_without_freeze_resets_streak(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 1))
        rewards.apply_completion(state, 10, date(2026, 9, 10))
        self.assertEqual(state["streak_days"], 1)

    def test_one_freeze_does_not_cover_multiple_missed_days(self):
        state = base_state()
        state.update({
            "last_active_date": "2026-09-01", "streak_days": 7,
            "freezes": 1, "freeze_progress": 0,
        })
        rewards.apply_completion(state, 10, date(2026, 9, 4))
        self.assertEqual(state["streak_days"], 1)
        self.assertEqual(state["freezes"], 0)

    def test_broken_streak_resets_freeze_progress(self):
        state = base_state()
        state.update({
            "last_active_date": "2026-09-01", "streak_days": 6,
            "freeze_progress": 6,
        })
        rewards.apply_completion(state, 10, date(2026, 9, 3))
        self.assertEqual(state["streak_days"], 1)
        self.assertEqual(state["freeze_progress"], 1)

    def test_late_completion_does_not_move_streak_backwards(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        rewards.apply_completion(state, 10, date(2026, 9, 17))
        self.assertEqual(state["last_active_date"], "2026-09-18")
        self.assertEqual(state["streak_days"], 1)
        self.assertEqual(state["_bonus"], 0)

    def test_category_counter_increments(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18), category="health")
        rewards.apply_completion(state, 10, date(2026, 9, 18), category="health")
        self.assertEqual(state["categories"]["health"], 2)

    def test_leveled_reports_only_this_calls_level_ups(self):
        state = base_state()
        state["xp_into_level"] = rewards.xp_to_next(1) - 1
        rewards.apply_completion(state, 1, date(2026, 9, 18))
        self.assertEqual(state["_leveled"], [2])
        rewards.apply_completion(state, 1, date(2026, 9, 18))
        # no further level-up this call -> must NOT still report the old one
        self.assertEqual(state["_leveled"], [])


class EffectiveStreakTests(unittest.TestCase):
    def test_active_today_is_unaffected(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        days, protected = rewards.effective_streak(state, date(2026, 9, 18))
        self.assertEqual(days, 1)
        self.assertFalse(protected)

    def test_gap_without_freeze_shows_as_broken_before_next_completion(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 1))
        days, protected = rewards.effective_streak(state, date(2026, 9, 5))
        self.assertEqual(days, 0)
        self.assertFalse(protected)
        # and the underlying ledger is untouched -- correction happens for
        # real only via apply_completion on the next completion
        self.assertEqual(state["streak_days"], 1)

    def test_gap_with_freeze_still_shows_protected(self):
        state = base_state()
        for d in range(1, 1 + rewards.FREEZE_EVERY):
            rewards.apply_completion(state, 10, date(2026, 9, d))
        self.assertGreaterEqual(state["freezes"], 1)
        days, protected = rewards.effective_streak(state, date(2026, 9, 1 + rewards.FREEZE_EVERY + 1))
        self.assertTrue(protected)
        self.assertEqual(days, state["streak_days"])

    def test_one_freeze_does_not_display_long_gap_as_protected(self):
        state = base_state()
        state.update({"last_active_date": "2026-09-01", "streak_days": 7, "freezes": 1})
        days, protected = rewards.effective_streak(state, date(2026, 9, 4))
        self.assertEqual(days, 0)
        self.assertFalse(protected)


class PollCompletionDateTests(unittest.TestCase):
    def test_poll_applies_records_on_their_completion_dates(self):
        records = [
            {"id": "2", "title": "today", "completed_at": "2026-09-18T12:00:00Z",
             "priority": 1, "source": "fake"},
            {"id": "1", "title": "yesterday", "completed_at": "2026-09-17T12:00:00Z",
             "priority": 1, "source": "fake"},
        ]
        backend = mock.Mock()
        backend.list_completions.return_value = records
        cfg = {"backend": "fake", "timezone": "UTC", "xp": rewards.DEFAULT_XP}
        state = base_state()
        state["baseline_at"] = "2026-09-16T00:00:00Z"
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(rewards, "load_backend", return_value=backend), \
             mock.patch.object(rewards, "local_today", return_value=date(2026, 9, 18)):
            rewards.cmd_poll(cfg, state, Path(tmp) / "ledger.json")
        self.assertEqual(state["streak_days"], 2)
        self.assertEqual(state["last_active_date"], "2026-09-18")

    def test_poll_ignores_future_dated_records(self):
        backend = mock.Mock()
        backend.list_completions.return_value = [{
            "id": "future", "title": "future", "completed_at": "2026-09-19T12:00:00Z",
            "priority": 1, "source": "fake",
        }]
        cfg = {"backend": "fake", "timezone": "UTC", "xp": rewards.DEFAULT_XP}
        state = base_state()
        state["baseline_at"] = "2026-09-16T00:00:00Z"
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(rewards, "load_backend", return_value=backend), \
             mock.patch.object(rewards, "local_today", return_value=date(2026, 9, 18)):
            payload = rewards.cmd_poll(cfg, state, Path(tmp) / "ledger.json")
        self.assertEqual(payload["batch"], [])
        self.assertEqual(state["tasks_completed"], 0)


class AchievementTests(unittest.TestCase):
    def test_first_spark_unlocks_on_first_task_and_pays_once(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        newly = rewards.check_achievements(state)
        self.assertIn("first_spark", state["achievements"])
        self.assertEqual(len(newly), 1)
        xp_after_first = state["total_xp"]
        # calling again with no new counters must not re-pay it
        newly_again = rewards.check_achievements(state)
        self.assertEqual(newly_again, [])
        self.assertEqual(state["total_xp"], xp_after_first)

    def test_earned_achievements_is_pure_read(self):
        state = base_state()
        rewards.apply_completion(state, 10, date(2026, 9, 18))
        before = json_copy(state)
        rewards.earned_achievements(state)
        rewards.earned_achievements(state)
        self.assertEqual(state, before)  # rendering never mutates or pays XP

    def test_earned_view_is_derived_from_counters_not_paid_bonus_list(self):
        state = base_state()
        state["tasks_completed"] = 1
        state["achievements"] = []
        earned = {key: unlocked for key, _label, unlocked, _cur, _target
                  in rewards.earned_achievements(state)}
        self.assertTrue(earned["first_spark"])
        self.assertEqual(state["achievements"], [])

    def test_category_ladders_are_generated_per_category(self):
        state = base_state()
        state["categories"] = {"health": 10}
        keys = {k for k, _l, _p, _t, _b in rewards.all_achievements(state)}
        self.assertIn("health_apprentice", keys)
        self.assertIn("health_master", keys)
        self.assertIn("health_expert", keys)

    def test_unknown_category_gets_no_ladder_until_seen(self):
        state = base_state()
        keys = {k for k, _l, _p, _t, _b in rewards.all_achievements(state)}
        self.assertFalse(any(k.startswith("ghost_") for k in keys))


class HighlightThrottleTests(unittest.TestCase):
    def test_caps_at_HIGHLIGHT_CAP_per_day(self):
        state = base_state()
        today = date(2026, 9, 18)
        grants = [rewards.take_highlight(state, today) for _ in range(rewards.HIGHLIGHT_CAP + 2)]
        self.assertEqual(sum(grants), rewards.HIGHLIGHT_CAP)

    def test_resets_on_a_new_day(self):
        state = base_state()
        for _ in range(rewards.HIGHLIGHT_CAP):
            rewards.take_highlight(state, date(2026, 9, 18))
        self.assertFalse(rewards.take_highlight(state, date(2026, 9, 18)))
        self.assertTrue(rewards.take_highlight(state, date(2026, 9, 19)))


class StatusRenderTests(unittest.TestCase):
    def test_compact_status_is_two_useful_lines(self):
        state = rewards.default_state("test")
        state.update({"streak_days": 3, "freezes": 1, "tasks_completed": 4, "total_xp": 80})
        payload = rewards.status_payload(state, "test", date(2026, 9, 18))

        rendered = rewards.render_status(payload, compact=True)

        self.assertEqual(len(rendered.splitlines()), 2)
        self.assertIn("Level", rendered)
        self.assertIn("3 day streak", rendered)
        self.assertIn("4 tasks", rendered)


class StoragedTests(unittest.TestCase):
    def test_atomic_write_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ledger.json"
            payload = {"a": 1, "b": [1, 2, 3]}
            rewards.atomic_write_json(p, payload)
            self.assertEqual(rewards.load_json(p, {}), payload)

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "missing.json"
            self.assertEqual(rewards.load_json(p, {"x": 1}), {"x": 1})

    def test_corrupt_file_is_quarantined_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ledger.json"
            p.write_text("{not valid json", encoding="utf-8")
            result = rewards.load_json(p, {"x": 1})
            self.assertEqual(result["x"], 1)
            self.assertIn("_recovered_from", result)
            backups = list(Path(tmp).glob("ledger.corrupt.*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not valid json")


def json_copy(d):
    import json
    return json.loads(json.dumps(d))


if __name__ == "__main__":
    unittest.main()
