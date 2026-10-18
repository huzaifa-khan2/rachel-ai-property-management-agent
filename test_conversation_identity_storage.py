"""
Offline regression coverage for making conversation_identity_key the
durable logical-conversation linkage for stored thread/message history
(rachel_event_memory.py: resolve_and_link_conversation_identity,
backfill_conversation_identity_for_legacy_thread,
link_conversation_identities; rachel_appfolio_inbox.py:
_store_verified_thread).

Pure DB/function tests against temporary sqlite files - no Playwright, no
live AppFolio.
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
    backfill_conversation_identity_for_legacy_thread,
    initialize_event_memory,
    link_conversation_identities,
    resolve_and_link_conversation_identity,
    store_communication,
)


class ResolveAndLinkTests(unittest.TestCase):
    def test_first_read_uses_the_freshly_computed_identity(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            canonical = resolve_and_link_conversation_identity(
                conversation_identity_key="identity-aaa",
                legacy_thread_id="list-legacy-1",
                db_path=db_path,
            )
        self.assertEqual(canonical, "identity-aaa")

    def test_rereads_are_idempotent_and_do_not_duplicate_links(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            for _ in range(5):
                canonical = resolve_and_link_conversation_identity(
                    conversation_identity_key="identity-aaa",
                    legacy_thread_id="list-legacy-1",
                    db_path=db_path,
                )
                self.assertEqual(canonical, "identity-aaa")
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM conversation_identity_links "
                    "WHERE legacy_thread_id = ?",
                    ("list-legacy-1",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)

    def test_restart_preserves_the_linkage(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            resolve_and_link_conversation_identity(
                conversation_identity_key="identity-aaa",
                legacy_thread_id="list-legacy-1",
                db_path=db_path,
            )
            # Simulate a full restart: brand new connection, no shared
            # Python state, same durable db file.
            canonical_after_restart = resolve_and_link_conversation_identity(
                conversation_identity_key="identity-BBB-drifted",
                legacy_thread_id="list-legacy-1",
                db_path=db_path,
            )
        self.assertEqual(canonical_after_restart, "identity-aaa")


class BackfillTests(unittest.TestCase):
    def test_existing_historical_messages_are_not_duplicated(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            legacy_thread_id = "list-legacy-first-city"
            for i in range(49):
                store_communication(
                    {
                        "communication_id": f"legacy-msg-{i}",
                        "source": "appfolio_communication_inbox_thread",
                        "source_thread_id": legacy_thread_id,
                        "source_message_id": f"src-{i}",
                        "direction": "inbound",
                        "message_text": f"historical message {i}",
                    },
                    db_path=db_path,
                )
            conn = sqlite3.connect(db_path)
            try:
                before_count = conn.execute(
                    "SELECT COUNT(*) FROM communications"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(before_count, 49)

            updated = backfill_conversation_identity_for_legacy_thread(
                conversation_identity_key="identity-canonical",
                legacy_thread_id=legacy_thread_id,
                db_path=db_path,
            )
            self.assertEqual(updated, 49)

            conn = sqlite3.connect(db_path)
            try:
                after_count = conn.execute(
                    "SELECT COUNT(*) FROM communications"
                ).fetchone()[0]
                linked_count = conn.execute(
                    "SELECT COUNT(*) FROM communications "
                    "WHERE conversation_identity_key = ?",
                    ("identity-canonical",),
                ).fetchone()[0]
            finally:
                conn.close()
            # No duplication - same row count, all now linked.
            self.assertEqual(after_count, 49)
            self.assertEqual(linked_count, 49)

    def test_backfill_never_overwrites_an_already_set_identity(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            store_communication(
                {
                    "communication_id": "already-linked",
                    "source": "appfolio_communication_inbox_thread",
                    "source_thread_id": "list-legacy-2",
                    "source_message_id": "src-x",
                    "direction": "inbound",
                    "message_text": "already linked",
                    "conversation_identity_key": "identity-original",
                },
                db_path=db_path,
            )
            backfill_conversation_identity_for_legacy_thread(
                conversation_identity_key="identity-different",
                legacy_thread_id="list-legacy-2",
                db_path=db_path,
            )
            conn = sqlite3.connect(db_path)
            try:
                value = conn.execute(
                    "SELECT conversation_identity_key FROM communications "
                    "WHERE communication_id = 'already-linked'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(value, "identity-original")


class ManualReconciliationTests(unittest.TestCase):
    def test_native_id_becoming_available_can_be_merged_into_existing_history(
        self,
    ):
        """
        Simulates: a conversation was tracked under a synthetic fallback
        identity for a while (49 historical messages), then a native
        AppFolio thread id becomes available. Merging must not duplicate
        the 49 messages, and future messages attach to the SAME durable
        conversation as the merged target.
        """
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            old_identity = "identity-synthetic-fallback"
            new_identity = "af-native-thread-42"
            for i in range(49):
                store_communication(
                    {
                        "communication_id": f"old-msg-{i}",
                        "source": "appfolio_communication_inbox_thread",
                        "source_thread_id": "list-legacy-3",
                        "source_message_id": f"src-{i}",
                        "direction": "inbound",
                        "message_text": f"historical {i}",
                        "conversation_identity_key": old_identity,
                    },
                    db_path=db_path,
                )

            # The 49 historical reads would have gone through this each
            # time, establishing the legacy_thread_id -> identity link.
            resolve_and_link_conversation_identity(
                conversation_identity_key=old_identity,
                legacy_thread_id="list-legacy-3",
                db_path=db_path,
            )

            updated = link_conversation_identities(
                from_identity_key=old_identity,
                to_identity_key=new_identity,
                db_path=db_path,
            )
            self.assertEqual(updated, 49)

            # The new native-id read stores its own (50th) message under
            # the new identity directly.
            store_communication(
                {
                    "communication_id": "new-msg-50",
                    "source": "appfolio_communication_inbox_thread",
                    "source_thread_id": "af-native-thread-42",
                    "source_message_id": "src-50",
                    "direction": "inbound",
                    "message_text": "new message",
                    "conversation_identity_key": new_identity,
                },
                db_path=db_path,
            )

            conn = sqlite3.connect(db_path)
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM communications"
                ).fetchone()[0]
                under_new = conn.execute(
                    "SELECT COUNT(*) FROM communications "
                    "WHERE conversation_identity_key = ?",
                    (new_identity,),
                ).fetchone()[0]
                under_old = conn.execute(
                    "SELECT COUNT(*) FROM communications "
                    "WHERE conversation_identity_key = ?",
                    (old_identity,),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(total, 50)
            self.assertEqual(under_new, 50)
            self.assertEqual(under_old, 0)

            # A subsequent resolve for the OLD legacy thread id (if ever
            # read again) now resolves through the merged link too.
            canonical = resolve_and_link_conversation_identity(
                conversation_identity_key=old_identity,
                legacy_thread_id="list-legacy-3",
                db_path=db_path,
            )
            self.assertEqual(canonical, new_identity)

    def test_merge_requires_both_keys(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with self.assertRaises(ValueError):
                link_conversation_identities(
                    from_identity_key="", to_identity_key="x",
                    db_path=db_path,
                )

    def test_merge_into_self_is_a_safe_no_op(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            updated = link_conversation_identities(
                from_identity_key="identity-same",
                to_identity_key="identity-same",
                db_path=db_path,
            )
        self.assertEqual(updated, 0)


class LegacyMigrationTests(unittest.TestCase):
    def test_migration_from_pre_identity_schema_is_idempotent_and_non_destructive(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "legacy.db")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE communications (
                    communication_id  TEXT PRIMARY KEY,
                    source            TEXT NOT NULL,
                    source_thread_id  TEXT,
                    source_message_id TEXT,
                    contact_name      TEXT,
                    contact_role      TEXT,
                    property_address  TEXT,
                    unit              TEXT,
                    occurred_at       TEXT,
                    direction         TEXT NOT NULL,
                    message_text      TEXT,
                    preview           TEXT,
                    summary           TEXT,
                    topic             TEXT,
                    related_wo        TEXT,
                    related_tenant    TEXT,
                    related_vendor    TEXT,
                    internal_user_id  TEXT,
                    first_seen_at     TEXT NOT NULL,
                    last_updated_at   TEXT NOT NULL,
                    processing_status TEXT NOT NULL,
                    metadata_json     TEXT NOT NULL DEFAULT '{}'
                );
                INSERT INTO communications (
                    communication_id, source, source_thread_id,
                    source_message_id, direction, first_seen_at,
                    last_updated_at, processing_status
                ) VALUES (
                    'legacy-comm-1', 'appfolio_communication_inbox_thread',
                    'list-legacy-old', 'src-1', 'inbound',
                    '2026-01-01T00:00:00', '2026-01-01T00:00:00', 'stored'
                );
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
                        "PRAGMA table_info(communications)"
                    ).fetchall()
                }
                self.assertIn("conversation_identity_key", columns)
                preserved = conn.execute(
                    "SELECT source_thread_id, processing_status FROM "
                    "communications WHERE communication_id = 'legacy-comm-1'"
                ).fetchone()
                self.assertEqual(
                    preserved, ("list-legacy-old", "stored")
                )
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertIn("conversation_identity_links", tables)
            finally:
                conn.close()


class StoreVerifiedThreadIdentityTests(unittest.TestCase):
    """Exercises _store_verified_thread end to end against a real db."""

    def test_new_message_attaches_to_same_conversation_as_existing_history(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            row = {
                "thread_id": "list-fc-thread",
                "contact_name": "First City Construction",
                "contact_role": "vendor",
                "property": "", "unit": "",
                "conversation_identity_key": "identity-fc-vendor",
            }
            for i in range(49):
                store_communication(
                    {
                        "communication_id": f"hist-{i}",
                        "source": "appfolio_communication_inbox_thread",
                        "source_thread_id": "list-fc-thread",
                        "source_message_id": f"hist-src-{i}",
                        "direction": "inbound",
                        "message_text": f"historical {i}",
                        "conversation_identity_key": "identity-fc-vendor",
                    },
                    db_path=db_path,
                )

            thread = {
                "messages": [],
                "normalized_messages": [{
                    "source_message_id": "new-src-50",
                    "message_fingerprint": "f" * 64,
                    "message_text": "new inbound message",
                    "timestamp": "August 18 9:00 AM",
                    "direction": "inbound",
                    "rendered_index": 0,
                    "sender_classification": "",
                }],
                "phone": "", "headings": [], "context_fields": [],
            }
            with patch.object(
                inbox, "classify_communication_ownership",
                return_value={
                    "classification": "vendor_owned",
                    "reason": "test", "owner_internal_user_id": "",
                },
            ):
                result = inbox._store_verified_thread(
                    row, thread, db_path=db_path
                )

            self.assertEqual(result["conversation_identity_key"], "identity-fc-vendor")
            self.assertEqual(result["messages_newly_stored"], 1)
            self.assertEqual(result["backfilled_legacy_message_rows"], 0)

            conn = sqlite3.connect(db_path)
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM communications "
                    "WHERE conversation_identity_key = ?",
                    ("identity-fc-vendor",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(total, 50)


if __name__ == "__main__":
    unittest.main()
