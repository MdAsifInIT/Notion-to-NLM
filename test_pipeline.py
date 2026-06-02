import unittest
from notion_handler import _process_rich_text, _render_block, _extract_properties, RenderedBlock
from state_manager import load_state, save_state
from gdocs_handler import _build_entry_requests
import tempfile
import os
from pathlib import Path
from datetime import datetime, timezone

class TestPipeline(unittest.TestCase):
    def test_process_rich_text(self):
        rich_texts = [
            {"plain_text": "Hello ", "annotations": {"bold": True}},
            {"plain_text": "world", "annotations": {"italic": True}}
        ]
        text, annotations = _process_rich_text(rich_texts)
        self.assertEqual(text, "Hello world")
        self.assertEqual(len(annotations), 2)
        self.assertEqual(annotations[0]["start"], 0)
        self.assertEqual(annotations[0]["end"], 6)
        self.assertTrue(annotations[0]["bold"])
        self.assertFalse(annotations[0]["italic"])
        
        self.assertEqual(annotations[1]["start"], 6)
        self.assertEqual(annotations[1]["end"], 11)
        self.assertFalse(annotations[1]["bold"])
        self.assertTrue(annotations[1]["italic"])

    def test_render_block_paragraph(self):
        block = {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"plain_text": "Test paragraph", "annotations": {"bold": True}}
                ]
            }
        }
        rendered = _render_block(block)
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered.text, "Test paragraph")
        self.assertEqual(rendered.block_type, "paragraph")
        self.assertEqual(len(rendered.annotations), 1)

    def test_render_block_todo(self):
        block = {
            "type": "to_do",
            "to_do": {
                "checked": True,
                "rich_text": [
                    {"plain_text": "Buy milk", "annotations": {"bold": True}}
                ]
            }
        }
        rendered = _render_block(block)
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered.text, "[x] Buy milk")
        self.assertEqual(rendered.block_type, "to_do")
        self.assertTrue(rendered.checked)
        # Verify prefix shift
        self.assertEqual(rendered.annotations[0]["start"], 4)
        self.assertEqual(rendered.annotations[0]["end"], 12)

    def test_extract_properties(self):
        page = {
            "properties": {
                "Tags": {
                    "type": "multi_select",
                    "multi_select": [{"name": "Reflections"}, {"name": "Work"}]
                },
                "Name": {
                    "type": "title",
                    "title": [{"plain_text": "My Title"}]
                },
                "Checked": {
                    "type": "checkbox",
                    "checkbox": True
                }
            }
        }
        props = _extract_properties(page)
        self.assertEqual(len(props), 2)  # Skipping Title property
        self.assertEqual(props[0]["name"], "Checked")
        self.assertEqual(props[0]["value"], "Yes")
        self.assertEqual(props[1]["name"], "Tags")
        self.assertEqual(props[1]["value"], "Reflections, Work")

    def test_state_manager_atomic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            ts = datetime.now(timezone.utc)
            save_state(state_file, ts, ["page_1", "page_2"])
            
            # Load and verify
            loaded_ts, loaded_ids = load_state(state_file)
            self.assertEqual(loaded_ts.isoformat(), ts.isoformat())
            self.assertEqual(loaded_ids, ["page_1", "page_2"])

    def test_build_entry_requests_no_properties(self):
        entry = {
            "title": "My Entry",
            "last_edited": "2026-06-03T00:00:00.000Z",
            "blocks": [
                RenderedBlock(text="Body text", block_type="paragraph", annotations=[])
            ]
        }
        requests, end_idx = _build_entry_requests(entry, 10)
        self.assertTrue(len(requests) >= 2)
        # First request should be title insertion
        self.assertEqual(requests[0]["insertText"]["text"], "\n\n📝  My Entry  |  2026-06-03T00:00:00.000Z\n")
        self.assertEqual(requests[0]["insertText"]["location"]["index"], 10)

if __name__ == "__main__":
    unittest.main()
