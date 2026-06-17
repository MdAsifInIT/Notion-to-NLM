import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import main
import sync_engine
from gdocs_handler import SyncResult, _build_entry_requests
from notion_handler import (
    RenderedBlock,
    _extract_properties,
    _process_rich_text,
    _render_block,
    extract_page_text,
    query_recent_pages,
)
from retry_utils import RetryConfig, retry_call
from state_manager import file_lock, load_state, save_state


class TransientError(Exception):
    status = 429


class FakeExecute:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeDocuments:
    def __init__(self, doc):
        self.doc = doc
        self.batches = []

    def get(self, documentId):
        return FakeExecute(self.doc)

    def batchUpdate(self, documentId, body):
        self.batches.append(body["requests"])
        return FakeExecute({})


class FakeGoogleService:
    def __init__(self, doc):
        self.documents_resource = FakeDocuments(doc)

    def documents(self):
        return self.documents_resource


class FakeChildren:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def list(self, block_id, **kwargs):
        self.calls.append((block_id, kwargs))
        cursor = kwargs.get("start_cursor")
        key = (block_id, cursor)
        return self.responses[key]


class FakeNotionClient:
    def __init__(self):
        self.query_calls = 0
        self.blocks = Mock()

    def request(self, **kwargs):
        self.query_calls += 1
        if self.query_calls == 1:
            raise TransientError("slow down")
        if self.query_calls == 2:
            return {
                "results": [{"id": "page-1"}],
                "has_more": True,
                "next_cursor": "cursor-2",
            }
        return {"results": [{"id": "page-2"}], "has_more": False}


class TestNotionParsing(unittest.TestCase):
    def test_process_rich_text_annotations(self):
        rich_texts = [
            {"plain_text": "Hello ", "annotations": {"bold": True}},
            {"plain_text": "world", "annotations": {"italic": True}},
            {"plain_text": "!", "annotations": {"color": "default"}},
        ]
        text, annotations = _process_rich_text(rich_texts)
        self.assertEqual(text, "Hello world!")
        self.assertEqual(len(annotations), 2)
        self.assertEqual(annotations[0]["start"], 0)
        self.assertEqual(annotations[0]["end"], 6)
        self.assertTrue(annotations[0]["bold"])
        self.assertEqual(annotations[1]["start"], 6)
        self.assertEqual(annotations[1]["end"], 11)
        self.assertTrue(annotations[1]["italic"])

    def test_render_todo_shifts_annotations(self):
        block = {
            "type": "to_do",
            "to_do": {
                "checked": True,
                "rich_text": [{"plain_text": "Buy milk", "annotations": {"bold": True}}],
            },
        }
        rendered = _render_block(block)
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered.text, "[x] Buy milk")
        self.assertEqual(rendered.annotations[0]["start"], 4)
        self.assertEqual(rendered.annotations[0]["end"], 12)

    def test_extract_properties_renders_additional_types(self):
        page = {
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Hidden"}]},
                "Status": {"type": "status", "status": {"name": "Active"}},
                "Files": {"type": "files", "files": [{"name": "brief.pdf"}]},
                "Relation": {"type": "relation", "relation": [{"id": "abc"}]},
                "Formula": {
                    "type": "formula",
                    "formula": {"type": "string", "string": "Calculated"},
                },
                "Unique": {"type": "unique_id", "unique_id": {"prefix": "N", "number": 7}},
            }
        }
        props = _extract_properties(page)
        self.assertEqual(
            {prop["name"]: prop["value"] for prop in props},
            {
                "Files": "brief.pdf",
                "Formula": "Calculated",
                "Relation": "abc",
                "Status": "Active",
                "Unique": "N-7",
            },
        )

    def test_query_recent_pages_retries_and_paginates(self):
        client = FakeNotionClient()
        with patch("notion_handler._get_client", return_value=client), patch("retry_utils.time.sleep"):
            pages = query_recent_pages("token", "database", datetime.now(timezone.utc))
        self.assertEqual([page["id"] for page in pages], ["page-1", "page-2"])
        self.assertEqual(client.query_calls, 3)

    def test_extract_page_text_recurses_child_blocks(self):
        children = FakeChildren(
            {
                ("page-1", None): {
                    "results": [
                        {
                            "id": "parent",
                            "type": "bulleted_list_item",
                            "has_children": True,
                            "bulleted_list_item": {"rich_text": [{"plain_text": "Parent"}]},
                        }
                    ],
                    "has_more": False,
                },
                ("parent", None): {
                    "results": [
                        {
                            "id": "child",
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"plain_text": "Child"}]},
                        }
                    ],
                    "has_more": False,
                },
            }
        )
        client = Mock()
        client.blocks.children = children
        page = {
            "id": "page-1",
            "last_edited_time": "2026-06-01T00:00:00.000Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Title"}]}},
        }
        with patch("notion_handler._get_client", return_value=client):
            entry = extract_page_text("token", page)
        self.assertEqual(entry["title"], "Title")
        self.assertEqual([block.text for block in entry["blocks"]], ["Parent", "Child"])
        self.assertEqual(entry["blocks"][1].depth, 1)


class TestGoogleDocsRequests(unittest.TestCase):
    def test_build_entry_requests_buffers_text_and_styles(self):
        entry = {
            "title": "My Entry",
            "last_edited": "2026-06-03T00:00:00.000Z",
            "properties": [{"name": "Status", "value": "Active"}],
            "blocks": [
                RenderedBlock(
                    text="Body text",
                    block_type="paragraph",
                    annotations=[{"start": 0, "end": 4, "bold": True}],
                ),
                RenderedBlock(text="Item", block_type="bulleted_list_item", annotations=[]),
            ],
        }
        requests, end_idx = _build_entry_requests(entry, 10)
        self.assertEqual(requests[0]["insertText"]["location"]["index"], 10)
        inserted = requests[0]["insertText"]["text"]
        self.assertIn("My Entry", inserted)
        self.assertIn("Status: Active", inserted)
        self.assertIn("Body text", inserted)
        self.assertEqual(end_idx, 10 + len(inserted))
        self.assertTrue(any("createParagraphBullets" in req for req in requests))
        self.assertTrue(any(req.get("updateTextStyle", {}).get("textStyle", {}).get("bold") for req in requests))

    def test_sync_engine_replaces_valid_named_range(self):
        doc = {
            "body": {"content": [{"endIndex": 50}]},
            "namedRanges": {
                "notion_page_abc": {
                    "namedRanges": [
                        {
                            "namedRangeId": "range-1",
                            "ranges": [{"startIndex": 5, "endIndex": 20}],
                        }
                    ]
                }
            },
        }
        service = FakeGoogleService(doc)
        requests = sync_engine.build_upsert_requests(
            service,
            "doc",
            "abc",
            lambda start: ([{"insertText": {"location": {"index": start}, "text": "hello"}}], start + 5),
            lambda _service, _doc: 50,
        )
        self.assertIn("deleteNamedRange", requests[0])
        self.assertIn("deleteContentRange", requests[1])
        self.assertEqual(requests[-1]["createNamedRange"]["name"], "notion_page_abc")

    def test_sync_engine_repairs_stale_named_range(self):
        doc = {
            "body": {"content": [{"endIndex": 10}]},
            "namedRanges": {
                "notion_page_abc": {
                    "namedRanges": [
                        {
                            "namedRangeId": "range-1",
                            "ranges": [{"startIndex": 5, "endIndex": 99}],
                        }
                    ]
                }
            },
        }
        service = FakeGoogleService(doc)
        plan = sync_engine.build_upsert_plan(
            service,
            "doc",
            "abc",
            lambda start: ([{"insertText": {"location": {"index": start}, "text": "hello"}}], start + 5),
            lambda _service, _doc: 10,
        )
        self.assertEqual(plan.action, "repair_append")
        self.assertIn("deleteNamedRange", plan.requests[0])
        self.assertFalse(any("deleteContentRange" in req for req in plan.requests))
        self.assertEqual(plan.insert_index, 9)


class TestStateAndRetry(unittest.TestCase):
    def test_state_manager_atomic_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            ts = datetime.now(timezone.utc)
            save_state(state_file, ts, ["page_2", "page_1", "page_1"])
            loaded_ts, loaded_ids = load_state(state_file)
            self.assertEqual(loaded_ts.isoformat(), ts.isoformat())
            self.assertEqual(loaded_ids, ["page_1", "page_2"])

    def test_corrupt_state_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("{bad json", encoding="utf-8")
            loaded_ts, loaded_ids = load_state(str(path), default_lookback_hours=1)
            self.assertIsInstance(loaded_ts, datetime)
            self.assertEqual(loaded_ids, [])
            self.assertTrue(list(Path(tmpdir).glob("state.json.corrupt-*")))

    def test_file_lock_creates_lock_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "run.lock"
            with file_lock(lock_path):
                self.assertTrue(lock_path.exists())

    def test_retry_call_retries_transient_status(self):
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] == 1:
                raise TransientError("retry")
            return "ok"

        with patch("retry_utils.time.sleep"):
            result = retry_call(flaky, config=RetryConfig(max_retries=2, base_delay_seconds=0.1))
        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)


class TestPipelineMain(unittest.TestCase):
    def _env(self, tmpdir):
        return {
            "NOTION_TOKEN": "token",
            "NOTION_DATABASE_ID": "database",
            "GOOGLE_DOC_ID": "doc",
            "GOOGLE_CREDENTIALS_FILE": "credentials.json",
            "STATE_FILE": str(Path(tmpdir) / "state.json"),
            "METRICS_FILE": str(Path(tmpdir) / "metrics.json"),
            "RUN_LOCK_FILE": str(Path(tmpdir) / "run.lock"),
            "DEFAULT_LOOKBACK_HOURS": "24",
            "STATE_SAFETY_OVERLAP_SECONDS": "0",
        }

    def test_run_once_syncs_properties_only_entry_and_advances_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, self._env(tmpdir), clear=False):
            original_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            save_state(os.environ["STATE_FILE"], original_ts, [])
            entry = {
                "title": "Properties only",
                "body": "",
                "blocks": [],
                "properties": [{"name": "Status", "value": "Active"}],
                "page_id": "page-1",
            }
            with patch("main.query_recent_pages", return_value=[{"id": "page-1"}]), patch(
                "main.extract_page_text", return_value=entry
            ), patch("main.sync_to_doc", return_value=SyncResult(synced=1)):
                result = main.run_once()

            loaded_ts, loaded_ids = load_state(os.environ["STATE_FILE"])
            self.assertTrue(result.checkpoint_advanced)
            self.assertEqual(result.pages_synced, 1)
            self.assertGreater(loaded_ts, original_ts)
            self.assertEqual(loaded_ids, ["page-1"])

    def test_run_once_does_not_advance_checkpoint_after_extract_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, self._env(tmpdir), clear=False):
            original_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            save_state(os.environ["STATE_FILE"], original_ts, [])
            entry = {
                "title": "Good",
                "body": "Body",
                "blocks": [RenderedBlock(text="Body", block_type="paragraph", annotations=[])],
                "properties": [],
                "page_id": "page-1",
            }

            def extract(page_token, page):
                if page["id"] == "page-2":
                    raise RuntimeError("boom")
                return entry

            with patch(
                "main.query_recent_pages",
                return_value=[{"id": "page-1"}, {"id": "page-2"}],
            ), patch("main.extract_page_text", side_effect=extract), patch(
                "main.sync_to_doc", return_value=SyncResult(synced=1)
            ):
                result = main.run_once()

            loaded_ts, loaded_ids = load_state(os.environ["STATE_FILE"])
            self.assertFalse(result.checkpoint_advanced)
            self.assertEqual(result.pages_failed, 1)
            self.assertEqual(loaded_ts, original_ts)
            self.assertEqual(loaded_ids, [])

    def test_run_once_no_pages_writes_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, self._env(tmpdir), clear=False):
            save_state(os.environ["STATE_FILE"], datetime(2026, 1, 1, tzinfo=timezone.utc), [])
            with patch("main.query_recent_pages", return_value=[]):
                result = main.run_once()
            metrics = json.loads(Path(os.environ["METRICS_FILE"]).read_text(encoding="utf-8"))
            self.assertTrue(result.checkpoint_advanced)
            self.assertEqual(metrics["last_sync_pages_synced"], 0)
            self.assertEqual(metrics["last_sync_pages_failed"], 0)


if __name__ == "__main__":
    unittest.main()
