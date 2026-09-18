import _pathsetup  # noqa: F401  must run before importing rewards

import tempfile
import unittest
from datetime import date
from pathlib import Path

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
        days, protected = rewards.effective_streak(state, date(2026, 9, 1 + rewards.FREEZE_EVERY + 2))
        self.assertTrue(protected)
        self.assertEqual(days, state["streak_days"])


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
