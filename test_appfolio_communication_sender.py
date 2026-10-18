import hashlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_appfolio_communication_sender as sender_module
from rachel_appfolio_inbox import _normalize_thread_messages
from rachel_appfolio_communication_sender import (
    AppFolioExactCommunicationTransport,
    CommunicationSendError,
    dry_run_send_intent,
    execute_send_intent,
    prepare_send_intent,
)
from rachel_communication_workflow import (
    create_response_draft_plan,
    store_response_draft,
)
from rachel_event_memory import (
    inbox_selector_key,
    initialize_event_memory,
    record_communication_state,
    store_communication,
)


class FakeTransport:
    def __init__(self, *, error=None, changes=None):
        self.error = error
        self.changes = changes or {}
        self.calls = []

    def send_exact(self, message, *, expected):
        self.calls.append((message, expected))
        if self.error:
            raise self.error
        result = {
            "exact_thread_preserved": True,
            "recipient_identity_digest": expected[
                "recipient_identity_digest"
            ],
            "outbound_message_digest": expected["message_digest"],
            "messages_after": expected["messages_before"] + 1,
            "sent_message_fingerprint": "sent-fingerprint",
        }
        result.update(self.changes)
        return result


class AppFolioCommunicationSenderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "rachel.db")
        initialize_event_memory(self.db_path)
        self.thread_id = "thread-safe"
        self.messages = [
            ("outbound", "Earlier answer"),
            ("inbound", "Current question"),
        ]
        for index, (direction, text) in enumerate(self.messages):
            store_communication(
                {
                    "source": "appfolio_communication_inbox_thread",
                    "source_thread_id": self.thread_id,
                    "source_message_id": f"message-{index}",
                    "direction": direction,
                    "message_text": text,
                    "processing_status": "thread_read_verified",
                    "metadata": {
                        "message_fingerprint": f"fingerprint-{index}",
                        "rendered_index": index,
                    },
                },
                self.db_path,
            )
        record_communication_state(
            {
                "thread_id": self.thread_id,
                "source_message_id": "current-event",
                "preview": "Current question",
                "timestamp": "now",
            },
            "thread_read_verified",
            db_path=self.db_path,
        )
        plan = create_response_draft_plan(
            self.thread_id,
            ownership={"classification": "rachel_owned"},
            authorization_reason="thread_owned_by_rachel",
            db_path=self.db_path,
        )
        self.draft = store_response_draft(
            plan, "Prepared answer", db_path=self.db_path
        )
        self.row = {
            "thread_id": self.thread_id,
            "contact_name": "Jamie Tenant",
            "contact_role": "tenant",
            "property": "100 Oak Street",
            "unit": "2",
            "list_detail": "Tenant · 100 Oak Street · 2",
            "preview": "Current question",
        }
        self.row["selector_key"] = inbox_selector_key(self.row)
        self.snapshot = {
            "thread_root_count": 1,
            "header_region_count": 1,
            "history_region_count": 1,
            "composer_region_count": 1,
            "composer_textarea_count": 1,
            "composer_value_length": 0,
            "send_control_count": 1,
            "send_control_disabled": True,
            "send_via_sms_label_count": 1,
            "message_structure_valid": True,
            "contact_name": "Jamie Tenant",
            "contact_role": "tenant",
            "detail_context": "Jamie Tenant tenant 100 Oak Street 2",
            "normalized_message_fingerprints": [
                "fingerprint-0", "fingerprint-1"
            ],
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def _prepare(self):
        return prepare_send_intent(
            self.draft["draft_id"],
            selected_row=self.row,
            authorization_evidence_id="owned-thread-policy-v1",
            db_path=self.db_path,
        )

    def test_prepare_and_dry_run_are_idempotent_and_ui_write_free(self):
        first = self._prepare()
        second = self._prepare()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["intent_id"], second["intent_id"])
        result = dry_run_send_intent(
            first["intent_id"],
            selected_row=self.row,
            thread_snapshot=self.snapshot,
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "dry_run_verified")
        self.assertEqual(result["composer_actions_attempted"], 0)
        self.assertEqual(result["send_actions_attempted"], 0)
        self.assertEqual(result["appfolio_writes_attempted"], 0)

    def test_selector_latest_preview_change_fails_closed(self):
        intent = self._prepare()
        changed = dict(self.row, preview="New unverified event")
        with self.assertRaisesRegex(
            CommunicationSendError, "selected_event_changed"
        ):
            dry_run_send_intent(
                intent["intent_id"],
                selected_row=changed,
                thread_snapshot=self.snapshot,
                db_path=self.db_path,
            )

    def test_opened_recipient_or_context_change_fails_closed(self):
        intent = self._prepare()
        for key, value, reason in (
            ("contact_name", "Different Person", "opened_contact_changed"),
            ("contact_role", "vendor", "opened_role_changed"),
            ("detail_context", "Jamie Tenant tenant", "opened_property_changed"),
        ):
            with self.subTest(key=key):
                changed = dict(self.snapshot, **{key: value})
                with self.assertRaisesRegex(CommunicationSendError, reason):
                    dry_run_send_intent(
                        intent["intent_id"],
                        selected_row=self.row,
                        thread_snapshot=changed,
                        db_path=self.db_path,
                    )

    def test_ambiguous_composer_nonempty_composer_and_history_change_fail(self):
        cases = (
            (
                dict(self.snapshot, composer_region_count=2),
                "semantic_composer_region_count_invalid",
            ),
            (dict(self.snapshot, composer_value_length=4), "composer_not_empty"),
            (
                dict(
                    self.snapshot,
                    normalized_message_fingerprints=["fingerprint-0"],
                ),
                "mounted_history_changed",
            ),
        )
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                fresh = self._prepare()
                with self.assertRaisesRegex(CommunicationSendError, reason):
                    dry_run_send_intent(
                        fresh["intent_id"],
                        selected_row=self.row,
                        thread_snapshot=snapshot,
                        db_path=self.db_path,
                    )

    def test_new_durable_event_makes_draft_and_intent_stale(self):
        record_communication_state(
            {
                "thread_id": self.thread_id,
                "source_message_id": "new-event",
                "preview": "New question",
                "timestamp": "later",
            },
            "list_observed_unopened",
            db_path=self.db_path,
        )
        with self.assertRaisesRegex(
            CommunicationSendError, "verified_full_thread_unavailable"
        ):
            self._prepare()

    def test_live_send_is_blocked_before_transport_by_default(self):
        intent = self._prepare()
        transport = FakeTransport()
        with self.assertRaisesRegex(CommunicationSendError, "live_send_blocked"):
            execute_send_intent(
                intent["intent_id"],
                selected_row=self.row,
                thread_snapshot=self.snapshot,
                transport=transport,
                db_path=self.db_path,
            )
        self.assertEqual(transport.calls, [])

    def test_verified_transport_executes_once_and_cannot_repeat(self):
        intent = self._prepare()
        transport = FakeTransport()
        with patch(
            "rachel_appfolio_communication_sender."
            "appfolio_communication_send_allowed",
            return_value=(True, "test-injected"),
        ):
            result = execute_send_intent(
                intent["intent_id"],
                selected_row=self.row,
                thread_snapshot=self.snapshot,
                transport=transport,
                db_path=self.db_path,
            )
            self.assertEqual(result["status"], "sent_verified")
            with self.assertRaisesRegex(
                CommunicationSendError, "send_intent_not_prepared_sent_verified"
            ):
                execute_send_intent(
                    intent["intent_id"],
                    selected_row=self.row,
                    thread_snapshot=self.snapshot,
                    transport=transport,
                    db_path=self.db_path,
                )
        self.assertEqual(len(transport.calls), 1)
        conn = sqlite3.connect(self.db_path)
        status, attempts = conn.execute(
            "SELECT status, attempt_count FROM communication_send_intents"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "sent_verified")
        self.assertEqual(attempts, 1)

    def test_transport_failure_is_never_automatically_retried(self):
        intent = self._prepare()
        transport = FakeTransport(error=RuntimeError("unsafe raw detail"))
        with patch(
            "rachel_appfolio_communication_sender."
            "appfolio_communication_send_allowed",
            return_value=(True, "test-injected"),
        ):
            with self.assertRaisesRegex(
                CommunicationSendError, "transport_RuntimeError"
            ):
                execute_send_intent(
                    intent["intent_id"],
                    selected_row=self.row,
                    thread_snapshot=self.snapshot,
                    transport=transport,
                    db_path=self.db_path,
                )
            with self.assertRaisesRegex(
                CommunicationSendError,
                "send_intent_not_prepared_sending_unverified",
            ):
                execute_send_intent(
                    intent["intent_id"],
                    selected_row=self.row,
                    thread_snapshot=self.snapshot,
                    transport=transport,
                    db_path=self.db_path,
                )
        self.assertEqual(len(transport.calls), 1)
        conn = sqlite3.connect(self.db_path)
        status, attempts, failure = conn.execute(
            "SELECT status, attempt_count, failure_reason "
            "FROM communication_send_intents"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "sending_unverified")
        self.assertEqual(attempts, 1)
        self.assertEqual(failure, "transport_RuntimeError")
        self.assertNotIn("unsafe raw detail", failure)

    def test_postcheck_mismatch_remains_ambiguous_not_sent(self):
        intent = self._prepare()
        wrong_digest = hashlib.sha256(b"wrong").hexdigest()
        transport = FakeTransport(
            changes={"outbound_message_digest": wrong_digest}
        )
        with patch(
            "rachel_appfolio_communication_sender."
            "appfolio_communication_send_allowed",
            return_value=(True, "test-injected"),
        ):
            with self.assertRaisesRegex(
                CommunicationSendError, "send_postcheck_message_mismatch"
            ):
                execute_send_intent(
                    intent["intent_id"],
                    selected_row=self.row,
                    thread_snapshot=self.snapshot,
                    transport=transport,
                    db_path=self.db_path,
                )
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM communication_send_intents"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "sending_unverified")

    def test_durable_context_is_rechecked_atomically_before_send_claim(self):
        intent = self._prepare()
        transport = FakeTransport()
        original_verify = sender_module.verify_send_preconditions

        def verify_then_receive(*args, **kwargs):
            verified = original_verify(*args, **kwargs)
            store_communication(
                {
                    "source": "appfolio_communication_inbox_thread",
                    "source_thread_id": self.thread_id,
                    "source_message_id": "message-2",
                    "direction": "inbound",
                    "message_text": "Newer question",
                    "processing_status": "thread_read_verified",
                    "metadata": {
                        "message_fingerprint": "fingerprint-2",
                        "rendered_index": 2,
                    },
                },
                self.db_path,
            )
            record_communication_state(
                {
                    "thread_id": self.thread_id,
                    "source_message_id": "newer-event",
                    "preview": "Newer question",
                    "timestamp": "later",
                },
                "thread_read_verified",
                db_path=self.db_path,
            )
            return verified

        with patch(
            "rachel_appfolio_communication_sender."
            "appfolio_communication_send_allowed",
            return_value=(True, "test-injected"),
        ), patch.object(
            sender_module,
            "verify_send_preconditions",
            side_effect=verify_then_receive,
        ):
            with self.assertRaisesRegex(
                CommunicationSendError,
                "durable_context_changed_before_claim",
            ):
                execute_send_intent(
                    intent["intent_id"],
                    selected_row=self.row,
                    thread_snapshot=self.snapshot,
                    transport=transport,
                    db_path=self.db_path,
                )
        self.assertEqual(transport.calls, [])
        conn = sqlite3.connect(self.db_path)
        status, attempts = conn.execute(
            "SELECT status, attempt_count FROM communication_send_intents"
        ).fetchone()
        conn.close()
        self.assertEqual((status, attempts), ("prepared", 0))

    def test_exact_transport_fills_and_clicks_once_then_semantically_verifies(self):
        page = MagicMock()
        frame = MagicMock()
        textarea = MagicMock()
        textarea.input_value.side_effect = ["", "Prepared answer"]
        send = MagicMock()
        send.is_disabled.return_value = False
        before_messages = [
            {"direction": "outbound", "message_text": "Earlier answer"},
            {"direction": "inbound", "message_text": "Current question"},
        ]
        before = {"message_count": 2, "messages": before_messages}
        after = {
            "messages": before_messages + [
                {"direction": "outbound", "message_text": "Prepared answer"},
            ]
        }
        transport = AppFolioExactCommunicationTransport(page, self.row)
        expected = {
            "messages_before": 2,
            "recipient_identity_digest": hashlib.sha256(
                "jamie tenant|tenant|100 oak street|2".encode("utf-8")
            ).hexdigest(),
            "message_digest": hashlib.sha256(
                b"Prepared answer"
            ).hexdigest(),
            "message_fingerprints": [
                item["message_fingerprint"]
                for item in _normalize_thread_messages(
                    self.thread_id, before_messages
                )
            ],
        }
        with patch.object(
            transport, "_semantic_frame", side_effect=[
                (frame, before), (frame, after)
            ]
        ), patch.object(
            transport,
            "_exact_composer_locators",
            return_value=(textarea, send),
        ):
            result = transport.send_exact(
                "Prepared answer", expected=expected
            )
        textarea.fill.assert_called_once_with("Prepared answer")
        send.click.assert_called_once_with()
        self.assertEqual(result["messages_after"], 3)
        self.assertEqual(
            result["outbound_message_digest"], expected["message_digest"]
        )
        page.wait_for_timeout.assert_not_called()

    def test_exact_transport_never_reclicks_when_postcheck_is_delayed(self):
        page = MagicMock()
        frame = MagicMock()
        textarea = MagicMock()
        textarea.input_value.side_effect = ["", "Prepared answer"]
        send = MagicMock()
        send.is_disabled.return_value = False
        before_messages = [
            {"direction": "outbound", "message_text": "Earlier answer"},
            {"direction": "inbound", "message_text": "Current question"},
        ]
        before = {"message_count": 2, "messages": before_messages}
        unchanged = {"messages": before_messages}
        transport = AppFolioExactCommunicationTransport(page, self.row)
        expected = {
            "messages_before": 2,
            "recipient_identity_digest": hashlib.sha256(
                "jamie tenant|tenant|100 oak street|2".encode("utf-8")
            ).hexdigest(),
            "message_digest": hashlib.sha256(
                b"Prepared answer"
            ).hexdigest(),
            "message_fingerprints": [
                item["message_fingerprint"]
                for item in _normalize_thread_messages(
                    self.thread_id, before_messages
                )
            ],
        }
        with patch.object(
            transport, "_semantic_frame", side_effect=(
                [(frame, before)] + [(frame, unchanged)] * 20
            )
        ), patch.object(
            transport,
            "_exact_composer_locators",
            return_value=(textarea, send),
        ):
            with self.assertRaisesRegex(
                CommunicationSendError,
                "send_postcheck_message_count_not_incremented",
            ):
                transport.send_exact("Prepared answer", expected=expected)
        textarea.fill.assert_called_once_with("Prepared answer")
        send.click.assert_called_once_with()
        self.assertEqual(page.wait_for_timeout.call_count, 19)

    def test_exact_transport_postcheck_requires_original_history_prefix(self):
        page = MagicMock()
        frame = MagicMock()
        textarea = MagicMock()
        textarea.input_value.side_effect = ["", "Prepared answer"]
        send = MagicMock()
        send.is_disabled.return_value = False
        before_messages = [
            {"direction": "outbound", "message_text": "Earlier answer"},
            {"direction": "inbound", "message_text": "Current question"},
        ]
        changed_after = {
            "messages": [
                {"direction": "outbound", "message_text": "Changed history"},
                {"direction": "inbound", "message_text": "Current question"},
                {"direction": "outbound", "message_text": "Prepared answer"},
            ]
        }
        expected = {
            "messages_before": 2,
            "recipient_identity_digest": hashlib.sha256(
                "jamie tenant|tenant|100 oak street|2".encode("utf-8")
            ).hexdigest(),
            "message_digest": hashlib.sha256(
                b"Prepared answer"
            ).hexdigest(),
            "message_fingerprints": [
                item["message_fingerprint"]
                for item in _normalize_thread_messages(
                    self.thread_id, before_messages
                )
            ],
        }
        transport = AppFolioExactCommunicationTransport(page, self.row)
        with patch.object(
            transport,
            "_semantic_frame",
            side_effect=(
                [(frame, {"message_count": 2, "messages": before_messages})]
                + [(frame, changed_after)] * 20
            ),
        ), patch.object(
            transport,
            "_exact_composer_locators",
            return_value=(textarea, send),
        ):
            with self.assertRaisesRegex(
                CommunicationSendError,
                "send_postcheck_history_prefix_changed",
            ):
                transport.send_exact("Prepared answer", expected=expected)
        send.click.assert_called_once_with()

    def test_exact_transport_blocks_changed_history_before_ui_mutation(self):
        page = MagicMock()
        frame = MagicMock()
        transport = AppFolioExactCommunicationTransport(page, self.row)
        expected = {
            "messages_before": 2,
            "recipient_identity_digest": hashlib.sha256(
                "jamie tenant|tenant|100 oak street|2".encode("utf-8")
            ).hexdigest(),
            "message_digest": hashlib.sha256(
                b"Prepared answer"
            ).hexdigest(),
            "message_fingerprints": [],
        }
        with patch.object(
            transport,
            "_semantic_frame",
            return_value=(frame, {"message_count": 3}),
        ), patch.object(
            transport, "_exact_composer_locators"
        ) as composer:
            with self.assertRaisesRegex(
                CommunicationSendError, "transport_history_count_changed"
            ):
                transport.send_exact("Prepared answer", expected=expected)
        composer.assert_not_called()

    def test_exact_transport_blocks_same_count_history_replacement(self):
        page = MagicMock()
        frame = MagicMock()
        expected_messages = [
            {"direction": "outbound", "message_text": "Earlier answer"},
            {"direction": "inbound", "message_text": "Current question"},
        ]
        replaced_messages = [
            {"direction": "outbound", "message_text": "Earlier answer"},
            {"direction": "inbound", "message_text": "Different question"},
        ]
        expected = {
            "messages_before": 2,
            "recipient_identity_digest": hashlib.sha256(
                "jamie tenant|tenant|100 oak street|2".encode("utf-8")
            ).hexdigest(),
            "message_digest": hashlib.sha256(
                b"Prepared answer"
            ).hexdigest(),
            "message_fingerprints": [
                item["message_fingerprint"]
                for item in _normalize_thread_messages(
                    self.thread_id, expected_messages
                )
            ],
        }
        transport = AppFolioExactCommunicationTransport(page, self.row)
        with patch.object(
            transport,
            "_semantic_frame",
            return_value=(
                frame,
                {"message_count": 2, "messages": replaced_messages},
            ),
        ), patch.object(
            transport, "_exact_composer_locators"
        ) as composer:
            with self.assertRaisesRegex(
                CommunicationSendError,
                "transport_history_fingerprints_changed",
            ):
                transport.send_exact("Prepared answer", expected=expected)
        composer.assert_not_called()

    def test_exact_transport_requires_one_semantic_thread_frame(self):
        valid = dict(self.snapshot, message_count=2)
        frame_one = MagicMock()
        frame_two = MagicMock()
        frame_one.evaluate.return_value = valid
        frame_two.evaluate.return_value = valid
        transport = AppFolioExactCommunicationTransport(
            MagicMock(), self.row
        )
        with patch(
            "rachel_appfolio_inbox._discover_inbox_frames",
            return_value=[frame_one, frame_two],
        ), patch(
            "rachel_appfolio_inbox._verify_thread_identity",
            return_value={"matched": True},
        ):
            with self.assertRaisesRegex(
                CommunicationSendError,
                "exact_semantic_thread_frame_ambiguous",
            ):
                transport._semantic_frame()


if __name__ == "__main__":
    unittest.main()
