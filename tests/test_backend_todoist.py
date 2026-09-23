import _pathsetup  # noqa: F401

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backends import todoist


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FindTokenTests(unittest.TestCase):
    def test_environment_variable_wins_over_files(self):
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "env-token"}, clear=False):
            token, src = todoist.find_token()
            self.assertEqual(token, "env-token")
            self.assertIsNone(src)

    def test_does_not_read_tokens_from_profile_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            envfile = Path(tmp) / ".env"
            envfile.write_text('TODOIST_API_TOKEN="file-token"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}, clear=False):
                os.environ.pop("TODOIST_API_TOKEN", None)
                token, src = todoist.find_token()
                self.assertIsNone(token)
                self.assertIsNone(src)

    def test_no_token_anywhere_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp, "HOME": tmp}, clear=False):
                os.environ.pop("TODOIST_API_TOKEN", None)
                token, src = todoist.find_token()
                self.assertIsNone(token)
                self.assertIsNone(src)


class ListCompletionsTests(unittest.TestCase):
    """list_completions merges two streams: the by_completion_date log
    (one-off completions) and, when a project_id is configured, each live
    task's completed_count (recurring completions, which the log endpoint
    often drops once a recurring task resets for its next occurrence). Each
    stream needs its own urlopen response, keyed by URL."""

    def _mock_urlopen(self, completed_items=None, tasks=None):
        completed_payload = json.dumps({"items": completed_items or []}).encode("utf-8")
        tasks_payload = json.dumps({"results": tasks or []}).encode("utf-8")

        def side_effect(req, timeout=30):
            body = tasks_payload if "/tasks?" in req.full_url else completed_payload
            return FakeResponse(body)
        return mock.patch.object(todoist.urllib.request, "urlopen", side_effect=side_effect)

    def test_filters_by_configured_project_and_inverts_priority_passthrough(self):
        items = [
            {"id": 1, "project_id": "P1", "content": "In scope", "completed_at": "2026-09-18T00:00:00Z", "priority": 4},
            {"id": 2, "project_id": "P2", "content": "Wrong project", "completed_at": "2026-09-18T00:00:00Z", "priority": 4},
        ]
        cfg = {"backend_options": {"project_id": "P1", "category": "health"}}
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             self._mock_urlopen(completed_items=items):
            out = todoist.list_completions("2026-09-17T00:00:00Z", cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "1")
        self.assertEqual(out[0]["priority"], "4")  # passed through raw, not remapped
        self.assertEqual(out[0]["category"], "health")

    def test_missing_backend_options_does_not_crash(self):
        items = [{"id": 1, "project_id": "P1", "content": "Task", "completed_at": "2026-09-18T00:00:00Z", "priority": 1}]
        cfg = {"backend_options": {}}
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             self._mock_urlopen(completed_items=items):
            out = todoist.list_completions("2026-09-17T00:00:00Z", cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["category"], "")

    def test_recurring_task_completed_count_is_picked_up_and_categorized(self):
        # The by_completion_date log has nothing (as it commonly doesn't for
        # a recurring task that just reset), but the live task shows a win.
        tasks = [{"id": 9, "content": "Floss", "priority": 2, "completed_count": 1}]
        cfg = {"backend_options": {"project_id": "P1", "category": "health"}}
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             self._mock_urlopen(completed_items=[], tasks=tasks):
            out = todoist.list_completions("2026-09-17T00:00:00Z", cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "9")
        self.assertEqual(out[0]["category"], "health")

    def test_a_task_appearing_in_both_streams_the_same_day_is_not_double_counted(self):
        completed_items = [{"id": 9, "project_id": "P1", "content": "Floss",
                            "completed_at": "2026-09-18T08:00:00Z", "priority": 2}]
        tasks = [{"id": 9, "content": "Floss", "priority": 2, "completed_count": 1,
                 "completed_at": "2026-09-18T20:00:00Z"}]
        cfg = {"backend_options": {"project_id": "P1"}}
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             self._mock_urlopen(completed_items=completed_items, tasks=tasks):
            out = todoist.list_completions("2026-09-17T00:00:00Z", cfg)
        self.assertEqual(len(out), 1)

    def test_zero_completed_count_contributes_nothing(self):
        tasks = [{"id": 9, "content": "Floss", "priority": 2, "completed_count": 0}]
        cfg = {"backend_options": {"project_id": "P1"}}
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             self._mock_urlopen(completed_items=[], tasks=tasks):
            out = todoist.list_completions("2026-09-17T00:00:00Z", cfg)
        self.assertEqual(out, [])

    def test_no_project_id_skips_the_tasks_stream_entirely(self):
        """Without a project_id there's no scoped task list to page through,
        so only stream A (by_completion_date) should ever be queried."""
        items = [{"id": 1, "content": "Task", "completed_at": "2026-09-18T00:00:00Z", "priority": 1}]
        cfg = {"backend_options": {}}
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             mock.patch.object(todoist.urllib.request, "urlopen",
                               return_value=FakeResponse(json.dumps({"items": items}).encode())) as m:
            out = todoist.list_completions("2026-09-17T00:00:00Z", cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(m.call_count, 1)

    def test_no_token_raises_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp, "HOME": tmp}, clear=False):
                os.environ.pop("TODOIST_API_TOKEN", None)
                with self.assertRaises(RuntimeError) as ctx:
                    todoist.list_completions("", {"backend_options": {}})
                self.assertIn("TODOIST_API_TOKEN", str(ctx.exception))

    def test_completed_stream_follows_cursor_and_filters_on_server(self):
        pages = [
            {"items": [{"id": 1, "project_id": "P1", "content": "A",
                        "completed_at": "2026-09-18T08:00:00Z"}],
             "next_cursor": "page-2"},
            {"items": [{"id": 2, "project_id": "P1", "content": "B",
                        "completed_at": "2026-09-18T09:00:00Z"}],
             "next_cursor": None},
        ]
        urls = []

        def fake_get(url, token):
            urls.append(url)
            if "/tasks?" in url:
                return {"results": [], "next_cursor": None}
            return pages.pop(0)

        cfg = {"backend_options": {"project_id": "P1"}}
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             mock.patch.object(todoist, "_get", side_effect=fake_get):
            out = todoist.list_completions("2026-09-17T00:00:00Z", cfg)
        self.assertEqual({r["id"] for r in out}, {"1", "2"})
        self.assertIn("project_id=P1", urls[0])
        self.assertIn("cursor=page-2", urls[1])


class ListProjectsTests(unittest.TestCase):
    def test_list_projects_follows_cursor(self):
        pages = [
            {"results": [{"id": "1", "name": "Zed"}], "next_cursor": "next"},
            {"results": [{"id": "2", "name": "Alpha"}], "next_cursor": None},
        ]
        with mock.patch.dict(os.environ, {"TODOIST_API_TOKEN": "tok"}, clear=False), \
             mock.patch.object(todoist, "_get", side_effect=pages) as get:
            projects = todoist.list_projects()
        self.assertEqual([p["name"] for p in projects], ["Alpha", "Zed"])
        self.assertIn("cursor=next", get.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
