import _pathsetup  # noqa: F401

import json as jsonlib
import tempfile
import unittest
from pathlib import Path

from backends import json as json_backend  # regression test for the module-name fix:
# this used to be backends/jsonl.py while rewards.load_backend() imported
# "backends.json" -- --backend json was broken for every user until the file
# was renamed to match.


class JsonArrayTests(unittest.TestCase):
    def test_reads_a_json_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "completions.json"
            f.write_text(jsonlib.dumps([
                {"id": "1", "title": "Task one", "completed_at": "2026-09-18T00:00:00Z",
                 "priority": "3", "category": "work"},
            ]), encoding="utf-8")
            out = json_backend.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["source"], "json")
            self.assertEqual(out[0]["category"], "work")

    def test_missing_id_or_completed_at_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "completions.json"
            f.write_text(jsonlib.dumps([
                {"title": "no id"},
                {"id": "1", "title": "no date"},
                {"id": "2", "completed_at": "2026-09-18T00:00:00Z"},
            ]), encoding="utf-8")
            out = json_backend.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual([r["id"] for r in out], ["2"])

    def test_missing_file_returns_empty_list(self):
        out = json_backend.list_completions("", {"backend_options": {"path": "/no/such/file.json"}})
        self.assertEqual(out, [])

    def test_non_object_array_entries_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "mixed.json"
            f.write_text(jsonlib.dumps([
                "bad", None,
                {"id": "ok", "completed_at": "2026-09-18T00:00:00Z"},
            ]), encoding="utf-8")
            out = json_backend.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual([r["id"] for r in out], ["ok"])

    def test_directory_path_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = json_backend.list_completions(
                "", {"backend_options": {"path": tmp}})
            self.assertEqual(out, [])

    def test_records_older_than_the_overlap_window_are_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "history.json"
            f.write_text(jsonlib.dumps([
                {"id": "old", "completed_at": "2026-09-01T00:00:00Z"},
                {"id": "recent", "completed_at": "2026-09-17T12:00:00Z"},
            ]), encoding="utf-8")
            out = json_backend.list_completions(
                "2026-09-18T00:00:00Z", {"backend_options": {"path": str(f)}})
            self.assertEqual([r["id"] for r in out], ["recent"])


class NdjsonTests(unittest.TestCase):
    def test_reads_newline_delimited_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "completions.ndjson"
            lines = "\n".join(jsonlib.dumps({"id": str(i), "completed_at": "2026-09-18T00:00:00Z"})
                              for i in range(3))
            f.write_text(lines, encoding="utf-8")
            out = json_backend.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(len(out), 3)

    def test_blank_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "empty.json"
            f.write_text("", encoding="utf-8")
            out = json_backend.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual(out, [])

    def test_one_invalid_line_does_not_abort_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "mixed.ndjson"
            f.write_text(
                '{"id":"1","completed_at":"2026-09-18T00:00:00Z"}\n'
                'not json\n'
                '{"id":"2","completed_at":"2026-09-18T01:00:00Z"}\n',
                encoding="utf-8",
            )
            out = json_backend.list_completions("", {"backend_options": {"path": str(f)}})
            self.assertEqual([r["id"] for r in out], ["1", "2"])


class ValidationTests(unittest.TestCase):
    def test_rejects_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("regular file", json_backend.validate_file(Path(tmp)))

    def test_reports_the_invalid_ndjson_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "bad.ndjson"
            f.write_text('{"id":"1"}\nnot json\n', encoding="utf-8")
            self.assertIn("line 2", json_backend.validate_file(f))

    def test_accepts_a_json_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "ok.json"
            f.write_text("[]", encoding="utf-8")
            self.assertIsNone(json_backend.validate_file(f))


if __name__ == "__main__":
    unittest.main()
