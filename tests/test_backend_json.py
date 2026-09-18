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


if __name__ == "__main__":
    unittest.main()
