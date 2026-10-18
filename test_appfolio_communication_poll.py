import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_appfolio_inbox as inbox
import rachel_appfolio_inbox_dom_diagnostic as dom_diagnostic
import rachel_db_init
import rachel_inbox_monitor as inbox_monitor
from rachel_event_memory import record_communication_state, store_communication


class FakeInboxFrame:
    def __init__(
        self, url, *, rows=None, diagnostics=None, structure=None, name=""
    ):
        self.url = url
        self.name = name
        self.rows = rows or []
        self.diagnostics = diagnostics or {}
        self.structure = structure or {}
        self.evaluated = []

    def evaluate(self, script):
        self.evaluated.append(script)
        if script == inbox._INBOX_FRAME_DIAGNOSTICS_SCRIPT:
            return self.diagnostics
        if script == inbox._INBOX_LIGHTWEIGHT_ROWS_SCRIPT:
            return self.rows
        if script == inbox._INBOX_ROW_STRUCTURE_DIAGNOSTIC_SCRIPT:
            return self.structure
        raise AssertionError("Unexpected frame script")


class FakeFrameAwarePage:
    def __init__(self, frame_snapshots):
        self.frame_snapshots = frame_snapshots
        self.frame_reads = 0
        self.waits = []

    @property
    def frames(self):
        index = min(self.frame_reads, len(self.frame_snapshots) - 1)
        self.frame_reads += 1
        return self.frame_snapshots[index]

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class AppFolioCommunicationPollTests(unittest.TestCase):
    def row(self):
        return {
            "thread_id": "thread-1",
            "source_message_id": "message-1",
            "href": "/texting/inbox/thread-1",
            "contact_name": "Jamie Tenant",
            "contact_role": "tenant",
            "property": "100 Oak Street",
            "unit": "2",
            "preview": "The barking dog is still a nuisance",
            "timestamp": "August 8 10:00 AM",
            "unread": True,
        }

    def test_thread_id_parsing_handles_relative_and_absolute_urls(self):
        cases = {
            "/texting/inbox/thread-17": "thread-17",
            "/texting/inbox/thread-17?filter=unread": "thread-17",
            "https://example.appfolio.com/texting/inbox/abc%2D123#latest": "abc-123",
            "/texting/inbox?conversation_id=thread-18": "thread-18",
            "/texting/inbox?threadId=thread%2D19": "thread-19",
            "/texting/inbox": "",
            "/maintenance/work_orders/17": "",
            "": "",
        }
        for href, expected in cases.items():
            with self.subTest(href=href):
                self.assertEqual(
                    inbox.appfolio_thread_id_from_href(href), expected
                )

    def test_lightweight_rows_use_python_url_parser_and_filter_navigation(self):
        page = MagicMock()
        page.evaluate.return_value = [
            {"href": "/texting/inbox/thread-1", "preview": "one"},
            {
                "href": (
                    "https://example.appfolio.com/texting/inbox/thread-2"
                    "?filter=unread"
                ),
                "preview": "two",
            },
            {"href": "/texting/inbox", "preview": "navigation"},
        ]
        rows = inbox._read_lightweight_inbox_rows(page)
        self.assertEqual(
            [row["thread_id"] for row in rows],
            ["thread-1", "thread-2"],
        )
        extraction_script = page.evaluate.call_args.args[0]
        self.assertNotIn("href.match(", extraction_script)
        self.assertIn("ul.list-group > button", extraction_script)
        self.assertIn("li.list-group-item", extraction_script)
        self.assertIn("pseudoVisible", extraction_script)
        self.assertIn(r"/\b(TENANT|VENDOR|OWNER|RESIDENT)\b/i", extraction_script)
        self.assertNotIn("\x08", extraction_script)

    def test_list_metadata_gets_stable_synthetic_identities(self):
        raw = {
            "href": "",
            "contact_name": "Jamie Tenant",
            "contact_role": "tenant",
            "list_detail": "tenant",
            "preview": "Latest list preview",
            "timestamp": "2h",
        }
        first = inbox._normalize_lightweight_rows([raw])[0]
        second = inbox._normalize_lightweight_rows([dict(raw)])[0]
        self.assertTrue(first["thread_id"].startswith("list-"))
        self.assertTrue(
            first["source_message_id"].startswith("list-event-")
        )
        self.assertEqual(first["thread_id"], second["thread_id"])
        self.assertEqual(
            first["source_message_id"], second["source_message_id"]
        )

        later_relative_time = dict(raw, timestamp="3h")
        later = inbox._normalize_lightweight_rows([later_relative_time])[0]
        self.assertEqual(first["timestamp"], "")
        self.assertEqual(first["thread_id"], later["thread_id"])
        self.assertEqual(
            first["source_message_id"], later["source_message_id"]
        )

        changed = dict(raw, preview="A newer list preview")
        newer = inbox._normalize_lightweight_rows([changed])[0]
        self.assertEqual(first["thread_id"], newer["thread_id"])
        self.assertNotEqual(
            first["source_message_id"], newer["source_message_id"]
        )

    def test_filter_scope_reset_is_verified_and_conversation_safe(self):
        page = MagicMock()
        page.evaluate.return_value = {
            "status": "full_scope_verified",
            "verified_full_scope": True,
            "dropdown_triggers_checked": 2,
            "filter_controls_found": 2,
            "filter_clear_actions": 1,
            "filter_controls_already_clear": 1,
            "conversation_rows_before": 1,
            "conversation_rows_after": 8,
            "conversation_buttons_clicked": 0,
        }
        result = inbox.ensure_appfolio_inbox_full_monitoring_scope(page)
        self.assertTrue(result["verified_full_scope"])
        page.evaluate.assert_called_once_with(
            inbox._INBOX_FILTER_SCOPE_RESET_SCRIPT
        )
        script = inbox._INBOX_FILTER_SCOPE_RESET_SCRIPT
        self.assertIn("element.closest('ul.list-group')", script)
        self.assertIn("element.querySelector('li.list-group-item')", script)
        self.assertEqual(script.count("element.click()"), 1)

    def test_filter_scope_reset_fails_closed_when_unverified(self):
        page = MagicMock()
        page.evaluate.return_value = {
            "status": "full_scope_unverified",
            "verified_full_scope": False,
            "conversation_buttons_clicked": 0,
        }
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            inbox.ensure_appfolio_inbox_full_monitoring_scope(page)

    def test_empty_row_diagnostics_cannot_break_poll(self):
        page = MagicMock()
        page.evaluate.side_effect = [[], RuntimeError("diagnostic unavailable")]
        self.assertEqual(inbox._read_lightweight_inbox_rows(page), [])

    def test_lightweight_extraction_reads_rows_from_child_iframe(self):
        main_frame = FakeInboxFrame(
            "https://example.appfolio.com/texting/inbox",
            diagnostics={"iframe_elements": 1, "href_inbox": 2},
        )
        inbox_frame = FakeInboxFrame(
            "https://example.appfolio.com/communication/inbox-frame?token=hidden",
            name="communications",
            diagnostics={"candidate_rows": 1, "href_inbox": 1},
            rows=[{"href": "/texting/inbox/frame-thread-1"}],
        )
        page = FakeFrameAwarePage([[main_frame, inbox_frame]])
        rows = inbox._read_lightweight_inbox_rows(page)
        self.assertEqual(rows[0]["thread_id"], "frame-thread-1")
        self.assertEqual(page.waits, [])
        self.assertIn(
            inbox._INBOX_LIGHTWEIGHT_ROWS_SCRIPT,
            inbox_frame.evaluated,
        )

    def test_dynamic_retry_discovers_late_iframe(self):
        main_frame = FakeInboxFrame(
            "https://example.appfolio.com/texting/inbox"
        )
        late_frame = FakeInboxFrame(
            "https://example.appfolio.com/communication/inbox-frame",
            rows=[{"href": "/texting/inbox/late-thread"}],
        )
        page = FakeFrameAwarePage(
            [[main_frame], [main_frame, late_frame]]
        )
        rows = inbox._read_lightweight_inbox_rows(page)
        self.assertEqual(rows[0]["thread_id"], "late-thread")
        self.assertEqual(page.waits, [1500])

    def test_frame_diagnostics_strip_query_and_never_log_row_text(self):
        frame = FakeInboxFrame(
            "https://example.appfolio.com/texting/inbox?token=secret",
            diagnostics={"candidate_rows": 3, "semantic_rows": 8},
        )
        page = FakeFrameAwarePage([[frame]])
        output = io.StringIO()
        with redirect_stdout(output):
            diagnostics = inbox.diagnose_appfolio_inbox_structure(page)
        logged = output.getvalue()
        self.assertEqual(
            diagnostics[0]["location"],
            "https://example.appfolio.com/texting/inbox",
        )
        self.assertIn("candidate_rows=3", logged)
        self.assertNotIn("token=secret", logged)

    def test_row_structure_diagnostic_is_frame_aware_and_sanitized(self):
        frame = FakeInboxFrame(
            "https://example.appfolio.com/texting/inbox?token=secret",
            structure={
                "schema_version": 1,
                "sanitized": True,
                "candidate_group_count": 1,
                "candidate_groups": [
                    {
                        "sample_rows": [
                            {
                                "text_slots": [
                                    {
                                        "placeholder": "<TEXT_SLOT_1>",
                                        "character_count": 12,
                                    }
                                ],
                                "identity_candidates": [
                                    {
                                        "structural_attributes": [
                                            {
                                                "attribute_name": "data-id",
                                                "value_token": "VALUE_1",
                                                "value_kind": "numeric",
                                                "value_length": 6,
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ],
            },
        )
        report = inbox.diagnose_appfolio_inbox_row_structure(
            FakeFrameAwarePage([[frame]])
        )
        self.assertTrue(report["sanitized"])
        self.assertEqual(report["frame_count"], 1)
        self.assertEqual(
            report["frames"][0]["location"],
            "https://example.appfolio.com/texting/inbox",
        )
        serialized = str(report)
        self.assertIn("<TEXT_SLOT_1>", serialized)
        self.assertIn("VALUE_1", serialized)
        self.assertNotIn("token=secret", serialized)

    def test_row_structure_validator_rejects_raw_content_fields(self):
        with self.assertRaisesRegex(ValueError, "unsafe DOM diagnostic"):
            inbox._validate_sanitized_dom_diagnostic(
                {"message_text": "must never be returned"}
            )

    def test_dom_diagnostic_runner_writes_only_sanitized_report(self):
        with tempfile.TemporaryDirectory() as root:
            manager = MagicMock()
            page = MagicMock()
            manager.operation.return_value.__enter__.return_value = page
            manager.metrics = {
                "context_starts": 1,
                "navigations": 1,
                "restarts": 0,
                "successful_logins": 1,
            }
            report = {
                "schema_version": 1,
                "sanitized": True,
                "frame_count": 1,
                "frames": [
                    {
                        "structure": {"candidate_group_count": 2}
                    }
                ],
            }
            with patch.dict(
                os.environ,
                {
                    "RACHEL_TEST_MODE": "true",
                    "RACHEL_APPFOLIO_QA_ENABLED": "false",
                },
                clear=False,
            ), patch.object(
                dom_diagnostic,
                "get_appfolio_browser_manager",
                return_value=manager,
            ), patch.object(
                dom_diagnostic,
                "get_downloads_dir",
                return_value=root,
            ), patch.object(
                dom_diagnostic,
                "diagnose_appfolio_inbox_row_structure",
                return_value=report,
            ), patch.object(
                dom_diagnostic,
                "_read_lightweight_inbox_rows",
                return_value=[
                    {
                        "thread_id": "list-thread-hash",
                        "source_message_id": "list-event-hash",
                        "unread": True,
                    }
                ],
            ), patch.object(
                dom_diagnostic,
                "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={
                    "status": "full_scope_verified",
                    "verified_full_scope": True,
                    "filter_clear_actions": 1,
                },
            ):
                self.assertEqual(dom_diagnostic.main(), 0)

            page.goto.assert_called_once_with(
                inbox.INBOX_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout.assert_called_once_with(5000)
            manager.close.assert_called_once_with()
            output_files = list(
                Path(root).glob("appfolio_inbox_dom_sanitized_*.json")
            )
            self.assertEqual(len(output_files), 1)
            stored = json.loads(output_files[0].read_text(encoding="utf-8"))
            self.assertTrue(stored["sanitized"])
            self.assertEqual(stored["safety"]["threads_opened"], 0)
            self.assertEqual(stored["extraction_summary"]["rows_detected"], 1)
            self.assertEqual(
                stored["extraction_summary"]["unread_rows_detected"], 1
            )
            self.assertFalse(
                stored["extraction_summary"]
                ["raw_row_content_stored_in_report"]
            )

    def test_seen_list_event_does_not_open_thread(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            row = self.row()
            record_communication_state(row, "stored", db_path=db_path)
            page = MagicMock()
            original_db = inbox.DB_PATH
            inbox.DB_PATH = db_path
            metrics = {}
            try:
                with patch.object(inbox, "ensure_appfolio_inbox_full_monitoring_scope", return_value={"verified_full_scope": True}), patch.object(inbox, "_read_lightweight_inbox_rows", return_value=[row]), patch.object(
                    inbox, "_read_thread_event"
                ) as read_thread, patch.object(inbox, "human_delay", return_value=None):
                    self.assertEqual(
                        inbox.check_appfolio_inbox(page, metrics=metrics), 0
                    )
                    read_thread.assert_not_called()
                    self.assertEqual(page.goto.call_count, 1)
                    self.assertEqual(metrics["rows_seen"], 1)
                    self.assertEqual(metrics["newly_observed"], 0)
                    self.assertEqual(metrics["already_observed"], 1)
                    self.assertEqual(metrics["conversation_opens"], 0)
            finally:
                inbox.DB_PATH = original_db

    def test_new_unread_event_is_stored_from_list_without_opening_thread(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            row = self.row()
            page = MagicMock()
            original_db = inbox.DB_PATH
            inbox.DB_PATH = db_path
            metrics = {}
            try:
                with patch.object(inbox, "ensure_appfolio_inbox_full_monitoring_scope", return_value={"verified_full_scope": True}), patch.object(inbox, "_read_lightweight_inbox_rows", return_value=[row]), patch.object(
                    inbox, "_read_thread_event"
                ) as read_thread, patch.object(inbox, "find_related_work_order", return_value=None), patch.object(
                    inbox, "human_delay", return_value=None
                ):
                    self.assertEqual(
                        inbox.check_appfolio_inbox(page, metrics=metrics), 1
                    )
                    self.assertEqual(page.goto.call_count, 1)
                    read_thread.assert_not_called()
                    self.assertEqual(metrics["newly_observed"], 1)
                    self.assertEqual(metrics["stored_events"], 1)
                    self.assertEqual(
                        metrics["duplicate_event_fingerprints"], 0
                    )
                    self.assertEqual(metrics["appfolio_writes_attempted"], 0)
                    self.assertEqual(metrics["outbound_messages_attempted"], 0)
                conn = sqlite3.connect(db_path)
                stored = conn.execute(
                    "SELECT related_wo, message_text, processing_status, "
                    "metadata_json FROM communications"
                ).fetchone()
                conn.close()
                self.assertEqual(stored[0], "")
                self.assertIn("dog", stored[1])
                self.assertEqual(stored[2], "list_observed_unopened")
                self.assertIn('"thread_opened": false', stored[3])
            finally:
                inbox.DB_PATH = original_db

    def test_historical_question_finds_durable_non_wo_memory_first(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            original_init_path = rachel_db_init.DB_PATH
            original_monitor_path = inbox_monitor.DB_PATH
            rachel_db_init.DB_PATH = db_path
            inbox_monitor.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                store_communication(
                    {
                        "source": "appfolio_communication_inbox",
                        "contact_name": "Jamie Tenant",
                        "contact_role": "tenant",
                        "property_address": "100 Oak Street",
                        "unit": "2",
                        "direction": "inbound",
                        "message_text": "The neighbor's barking dog is a nuisance.",
                    },
                    db_path,
                )
                context = inbox_monitor.search_rachel_database(
                    "What did the tenant say about the neighbor's dog?",
                    internal_user_id="regional-manager",
                )
                self.assertIn("DURABLE COMMUNICATION MEMORY", context)
                self.assertIn("barking dog", context)
            finally:
                rachel_db_init.DB_PATH = original_init_path
                inbox_monitor.DB_PATH = original_monitor_path

    def test_memory_search_enforces_internal_user_portfolio_scope(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            original_init_path = rachel_db_init.DB_PATH
            original_monitor_path = inbox_monitor.DB_PATH
            rachel_db_init.DB_PATH = db_path
            inbox_monitor.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                conn = sqlite3.connect(db_path)
                conn.execute(
                    """
                    INSERT INTO internal_users (
                        user_id, display_name, email, role, portfolio_scope,
                        permission_scope, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        "casey-pm", "Casey PM", "casey@example.com",
                        "property_manager", '["100 Oak Street"]',
                        '["property_management"]', "now", "now",
                    ),
                )
                conn.commit()
                conn.close()
                store_communication(
                    {
                        "source": "appfolio_communication_inbox",
                        "source_message_id": "oak-dog",
                        "property_address": "100 Oak Street",
                        "direction": "inbound",
                        "message_text": "Oak tenant reported a barking dog.",
                    },
                    db_path,
                )
                store_communication(
                    {
                        "source": "appfolio_communication_inbox",
                        "source_message_id": "pine-dog",
                        "property_address": "200 Pine Avenue",
                        "direction": "inbound",
                        "message_text": "Pine tenant reported a barking dog.",
                        "internal_user_id": "casey-pm",
                    },
                    db_path,
                )
                context = inbox_monitor.search_rachel_database(
                    "What was reported about the barking dog?",
                    internal_user_id="casey-pm",
                )
                self.assertIn("Oak tenant", context)
                self.assertNotIn("Pine tenant", context)
            finally:
                rachel_db_init.DB_PATH = original_init_path
                inbox_monitor.DB_PATH = original_monitor_path


if __name__ == "__main__":
    unittest.main()
