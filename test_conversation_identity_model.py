"""
Offline regression coverage for the conversation identity / state split:

- conversation_identity_key(row): stable across new messages, never uses
  message preview/timestamp/unread/phone.
- conversation_state_key(row): the volatile fingerprint that changes when
  new activity arrives in an otherwise-unchanged conversation.
- qa_authorization_fingerprint(row): the (deliberately volatile) one-time
  QA/canary authorization fingerprint - an explicit alias of the existing
  inbox_selector_key, never Rachel's durable identity.

All pure function/DB-level tests - no Playwright, no live AppFolio.
"""

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

import rachel_appfolio_inbox as inbox
from rachel_event_memory import (
    conversation_identity_key,
    conversation_state_key,
    inbox_selector_key,
    initialize_event_memory,
    qa_authorization_fingerprint,
)


def row(**changes):
    base = {
        "thread_id": "",
        "source_message_id": "list-event-safe",
        "href": "",
        "contact_name": "First City Construction",
        "contact_role": "vendor",
        "list_detail": "Vendor · Unit 2",
        "assigned_user": "Rachel Asst",
        "property": "100 Oak Street",
        "unit": "2",
        "preview": "Initial inbound message",
        "timestamp": "August 12 10:00 AM",
        "unread": True,
        "phone": "555-0100",
    }
    base.update(changes)
    return base


class QaAuthorizationFingerprintTests(unittest.TestCase):
    def test_qa_authorization_fingerprint_is_an_explicit_alias(self):
        r = row()
        self.assertEqual(
            qa_authorization_fingerprint(r), inbox_selector_key(r)
        )

    def test_qa_authorization_fingerprint_changes_with_preview(self):
        a = row(preview="Message A")
        b = row(preview="Message B")
        self.assertNotEqual(
            qa_authorization_fingerprint(a), qa_authorization_fingerprint(b)
        )


class ConversationIdentityKeyTests(unittest.TestCase):
    def test_same_conversation_new_message_keeps_identity_unchanged(self):
        before = row(thread_id="af-native-thread-77", preview="First message")
        after = row(thread_id="af-native-thread-77", preview="Second message")
        self.assertEqual(
            conversation_identity_key(before), conversation_identity_key(after)
        )

    def test_state_fingerprint_changes_on_new_message(self):
        before = row(preview="First message")
        after = row(preview="Second message")
        self.assertNotEqual(
            conversation_state_key(before), conversation_state_key(after)
        )

    def test_identity_never_includes_preview_timestamp_or_unread(self):
        stable = row(thread_id="")  # synthetic-fallback path
        volatile_changed = row(
            thread_id="",
            preview="Completely different text",
            timestamp="Yesterday",
            unread=False,
        )
        self.assertEqual(
            conversation_identity_key(stable),
            conversation_identity_key(volatile_changed),
        )

    def test_native_appfolio_thread_id_is_preferred_when_available(self):
        r = row(thread_id="af-conversation-42", contact_name="Anything")
        self.assertEqual(conversation_identity_key(r), "af-conversation-42")

    def test_synthetic_list_prefixed_thread_id_is_not_treated_as_native(self):
        r1 = row(thread_id="list-abc123")
        r2 = row(thread_id="")
        # Both fall back to the same stable-field hash since neither has a
        # real native id (a "list-" prefix marks the synthetic fallback
        # _normalize_lightweight_rows itself would have produced).
        self.assertEqual(
            conversation_identity_key(r1), conversation_identity_key(r2)
        )

    def test_different_contacts_do_not_collide(self):
        a = row(thread_id="", contact_name="First City Construction")
        b = row(thread_id="", contact_name="Jamie Tenant")
        self.assertNotEqual(
            conversation_identity_key(a), conversation_identity_key(b)
        )

    def test_role_mismatch_alone_produces_a_different_identity(self):
        a = row(thread_id="", contact_role="vendor")
        b = row(thread_id="", contact_role="tenant")
        self.assertNotEqual(
            conversation_identity_key(a), conversation_identity_key(b)
        )

    def test_property_mismatch_alone_produces_a_different_identity(self):
        a = row(thread_id="", property="100 Oak Street")
        b = row(thread_id="", property="200 Elm Street")
        self.assertNotEqual(
            conversation_identity_key(a), conversation_identity_key(b)
        )

    def test_unit_mismatch_alone_produces_a_different_identity(self):
        a = row(thread_id="", unit="2")
        b = row(thread_id="", unit="5")
        self.assertNotEqual(
            conversation_identity_key(a), conversation_identity_key(b)
        )

    def test_shared_phone_number_does_not_cause_identity_collision(self):
        """
        The Client Owner / First City Construction test-account overlap:
        two genuinely different contacts sharing one phone number must not
        resolve to the same conversation identity. Neither key function
        reads phone at all, so this holds regardless of what AppFolio does
        with that number internally.
        """
        same_phone = "555-0100"
        first_city = row(
            thread_id="", contact_name="First City Construction",
            contact_role="vendor", phone=same_phone,
        )
        owner = row(
            thread_id="", contact_name="Client Owner",
            contact_role="owner", phone=same_phone,
        )
        self.assertEqual(first_city["phone"], owner["phone"])
        self.assertNotEqual(
            conversation_identity_key(first_city),
            conversation_identity_key(owner),
        )

    def test_phone_field_is_not_read_by_either_key_function(self):
        with_phone = row(phone="555-0100")
        without_phone = dict(with_phone)
        without_phone.pop("phone")
        self.assertEqual(
            conversation_identity_key(with_phone),
            conversation_identity_key(without_phone),
        )
        self.assertEqual(
            conversation_state_key(with_phone),
            conversation_state_key(without_phone),
        )


class OwnedThreadRoutingIdentityTests(unittest.TestCase):
    """Exercises the identity/state split through the real production
    routing function and its durable table, including a genuine process
    restart (a fresh sqlite connection with no shared in-memory state)."""

    @staticmethod
    def _owned_row(*, thread_id, preview, unread=True, contact_name="Vendor A"):
        r = row(
            thread_id=thread_id, preview=preview, unread=unread,
            contact_name=contact_name,
        )
        r["selector_key"] = inbox_selector_key(r)
        r["conversation_identity_key"] = conversation_identity_key(r)
        r["conversation_state_key"] = conversation_state_key(r)
        return r

    def test_new_message_detected_exactly_once(self):
        original = self._owned_row(thread_id="af-thread-1", preview="Msg 1")
        changed = self._owned_row(thread_id="af-thread-1", preview="Msg 2")
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with patch.object(
                inbox, "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox, "_read_lightweight_inbox_rows",
                side_effect=[[original], [changed], [changed]],
            ), patch.object(
                inbox, "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"appfolio_writes_attempted": 0},
            ) as inspect_thread:
                results = [
                    inbox.process_rachel_owned_appfolio_threads(
                        MagicMock(), db_path=db_path
                    )
                    for _ in range(3)
                ]
        self.assertEqual(results, [1, 1, 0])
        self.assertEqual(inspect_thread.call_count, 2)

    def test_reread_of_unchanged_conversation_deduplicates(self):
        unchanged = self._owned_row(thread_id="af-thread-2", preview="Same")
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with patch.object(
                inbox, "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox, "_read_lightweight_inbox_rows",
                return_value=[unchanged],
            ), patch.object(
                inbox, "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"appfolio_writes_attempted": 0},
            ) as inspect_thread:
                first = inbox.process_rachel_owned_appfolio_threads(
                    MagicMock(), db_path=db_path
                )
                second = inbox.process_rachel_owned_appfolio_threads(
                    MagicMock(), db_path=db_path
                )
                third = inbox.process_rachel_owned_appfolio_threads(
                    MagicMock(), db_path=db_path
                )
        self.assertEqual((first, second, third), (1, 0, 0))
        self.assertEqual(inspect_thread.call_count, 1)

    def test_restart_preserves_identity_and_processed_message_state(self):
        target = self._owned_row(thread_id="af-thread-3", preview="Msg 1")
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")

            with patch.object(
                inbox, "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox, "_read_lightweight_inbox_rows", return_value=[target],
            ), patch.object(
                inbox, "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"appfolio_writes_attempted": 0},
            ):
                processed = inbox.process_rachel_owned_appfolio_threads(
                    MagicMock(), db_path=db_path
                )
            self.assertEqual(processed, 1)

            # Simulate a full process restart: brand new sqlite connection,
            # no Python objects carried over except the durable db file.
            conn = sqlite3.connect(db_path)
            try:
                stored = conn.execute(
                    "SELECT conversation_identity_key, latest_state_key, "
                    "processing_status FROM appfolio_thread_selector_state"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(
                stored[0], conversation_identity_key(target)
            )
            self.assertEqual(stored[1], conversation_state_key(target))
            self.assertEqual(stored[2], "thread_read_verified")

            # Reread of the identical row after "restart" must not reopen.
            with patch.object(
                inbox, "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox, "_read_lightweight_inbox_rows", return_value=[target],
            ), patch.object(
                inbox, "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"appfolio_writes_attempted": 0},
            ) as inspect_thread:
                processed_again = inbox.process_rachel_owned_appfolio_threads(
                    MagicMock(), db_path=db_path
                )
            self.assertEqual(processed_again, 0)
            inspect_thread.assert_not_called()

    def test_different_contacts_are_never_conflated_by_routing(self):
        first_city = self._owned_row(
            thread_id="", preview="Job update",
            contact_name="First City Construction",
        )
        owner = self._owned_row(
            thread_id="", preview="Job update",
            contact_name="Client Owner",
        )
        self.assertNotEqual(
            first_city["conversation_identity_key"],
            owner["conversation_identity_key"],
        )
        with tempfile.TemporaryDirectory() as root, patch.object(
            inbox, "ensure_appfolio_inbox_full_monitoring_scope",
            return_value={"verified_full_scope": True},
        ), patch.object(
            inbox, "_read_lightweight_inbox_rows",
            return_value=[first_city, owner],
        ), patch.object(
            inbox, "inspect_one_appfolio_inbox_thread_read_only",
            return_value={"appfolio_writes_attempted": 0},
        ) as inspect_thread:
            processed = inbox.process_rachel_owned_appfolio_threads(
                MagicMock(), db_path=os.path.join(root, "rachel.db")
            )
        # Both are genuinely new/unknown conversations - both processed,
        # neither skipped as a false duplicate of the other.
        self.assertEqual(processed, 2)
        self.assertEqual(inspect_thread.call_count, 2)


class SchemaMigrationTests(unittest.TestCase):
    def test_legacy_table_without_new_columns_migrates_idempotently(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "legacy.db")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE appfolio_thread_selector_state (
                    selector_key      TEXT PRIMARY KEY,
                    thread_id         TEXT NOT NULL,
                    latest_event_key  TEXT NOT NULL DEFAULT '',
                    last_unread       INTEGER NOT NULL DEFAULT 0,
                    processing_status TEXT NOT NULL,
                    last_error        TEXT NOT NULL DEFAULT '',
                    updated_at        TEXT NOT NULL
                );
                INSERT INTO appfolio_thread_selector_state
                    (selector_key, thread_id, processing_status, updated_at)
                VALUES ('a' * 64, 'af-legacy-thread', 'thread_read_verified',
                        '2026-01-01T00:00:00');
                """
            )
            conn.commit()
            conn.close()

            initialize_event_memory(db_path)
            initialize_event_memory(db_path)  # idempotent rerun

            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(appfolio_thread_selector_state)"
                    ).fetchall()
                }
                self.assertIn("conversation_identity_key", columns)
                self.assertIn("latest_state_key", columns)
                preserved = conn.execute(
                    "SELECT thread_id, processing_status FROM "
                    "appfolio_thread_selector_state WHERE thread_id = "
                    "'af-legacy-thread'"
                ).fetchone()
                self.assertEqual(
                    preserved, ("af-legacy-thread", "thread_read_verified")
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
