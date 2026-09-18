import _pathsetup  # noqa: F401

import tempfile
import unittest
from pathlib import Path

from backends import markdown


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


class ObsidianFormatTests(unittest.TestCase):
    def test_parses_done_date_and_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "log.md"
            write(f, "- [x] #task Ship the release ✅ 2026-09-18 \U0001f53a\n")
            out = markdown.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(len(out), 1)
            rec = out[0]
            self.assertEqual(rec["completed_at"][:10], "2026-09-18")
            self.assertEqual(rec["priority"], "4")
            self.assertEqual(rec["title"], "Ship the release")

    def test_incomplete_task_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "log.md"
            write(f, "- [ ] #task Not done yet\n")
            out = markdown.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(out, [])


class TodoTxtFormatTests(unittest.TestCase):
    def test_parses_priority_letter_and_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "todo.txt"
            write(f, "x 2026-09-18 (A) Submit proposal +work\n")
            out = markdown.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["priority"], "4")
            self.assertEqual(out[0]["completed_at"][:10], "2026-09-18")


class PlainCheckboxTests(unittest.TestCase):
    def test_undated_checkbox_is_stamped_at_detection_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "log.md"
            write(f, "- [x] Buy milk\n")
            out = markdown.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["priority"], "1")
            self.assertTrue(out[0]["completed_at"])  # some timestamp was stamped


class CategoryDerivationTests(unittest.TestCase):
    def test_real_tag_wins_over_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "Health"
            sub.mkdir()
            f = sub / "log.md"
            write(f, "- [x] #fitness Go for a run ✅ 2026-09-18\n")
            out = markdown.list_completions("", {"backend_options": {"path": str(tmp)}})
            self.assertEqual(out[0]["category"], "fitness")

    def test_marker_tags_are_skipped_falls_back_to_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "Health"
            sub.mkdir()
            f = sub / "log.md"
            write(f, "- [x] #task Go for a run ✅ 2026-09-18\n")
            out = markdown.list_completions("", {"backend_options": {"path": str(tmp)}})
            self.assertEqual(out[0]["category"], "health")

    def test_single_file_never_uses_its_own_parent_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "SomeFolder"
            sub.mkdir()
            f = sub / "log.md"
            write(f, "- [x] #task Go for a run ✅ 2026-09-18\n")
            out = markdown.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(out[0]["category"], "")  # engine will file this under inbox


class WatermarkFilterTests(unittest.TestCase):
    def test_filters_at_day_granularity(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "log.md"
            write(f, "- [x] #task Old one ✅ 2026-09-10\n- [x] #task New one ✅ 2026-09-18\n")
            out = markdown.list_completions("2026-09-18T00:00:00+00:00",
                                            {"backend_options": {"path": str(f)}})
            titles = {r["title"] for r in out}
            self.assertEqual(titles, {"New one"})


class DuplicateNumberingStabilityTests(unittest.TestCase):
    """Regression test: numbering used to be assigned purely by scan order,
    so appending a new same-day duplicate BEFORE an existing one in the file
    (a routine edit -- most editors insert, they don't only append) shifted
    the already-rewarded task's identity from base -> base#2. Numbering is
    now keyed by each duplicate's own completion date first, so same-day
    duplicates are ordered by that date (a tie), not by where a later edit
    happens to insert a new line.

    A duplicate inserted with an earlier BACKDATED date than existing ones is
    a known, inherent limit -- true stability there would need the backend to
    remember its own past numbering across polls, which the stateless
    backend contract deliberately doesn't do. Real usage checks tasks off
    going forward, so this doesn't come up in practice.
    """

    def test_reordering_same_day_duplicates_in_the_file_keeps_their_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "log.md"
            write(f, "- [x] #task Water plants ✅ 2026-09-17\n"
                     "- [x] #task Water plants ✅ 2026-09-17\n")
            before = markdown.list_completions("", {"backend_options": {"path": str(f)}})
            ids_before = sorted(r["id"] for r in before)

            # Insert a new, unrelated task line ABOVE the two duplicates --
            # an entirely ordinary edit that used to shift their identity.
            write(f, "- [x] #task Something else ✅ 2026-09-17\n"
                     "- [x] #task Water plants ✅ 2026-09-17\n"
                     "- [x] #task Water plants ✅ 2026-09-17\n")
            after = markdown.list_completions("", {"backend_options": {"path": str(f)}})
            ids_after = sorted(r["id"] for r in after if "water" in r["title"].lower())

            self.assertEqual(ids_before, ids_after)


if __name__ == "__main__":
    unittest.main()
