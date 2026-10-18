"""
Offline regression coverage for making conversation_identity_key the
candidate-discovery and history-loading key for draft planning (closing
the gap flagged after the post-identity-refactor live canary:
process_pending_rachel_owned_response_drafts and
rachel_communication_workflow's draft-planning functions previously grouped
and looked up by the legacy source_thread_id only, which could surface one
real conversation as two draft candidates after a native-id transition).

Pure DB/function tests - no Playwright, no live AppFolio.
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
import rachel_communication_workflow as workflow
from rachel_event_memory import initialize_event_memory, store_communication


def _mark_thread_read_verified(db_path, *, thread_id="", identity_key=""):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO appfolio_thread_selector_state (
                selector_key, thread_id, processing_status, updated_at,
                conversation_identity_key
            ) VALUES (?, ?, 'thread_read_verified', '2026-01-01T00:00:00', ?)
            """,
            (f"selector-{thread_id or identity_key}", thread_id, identity_key),
        )
        conn.commit()
    finally:
        conn.close()


def _store(db_path, *, comm_id, thread_id, identity_key, rendered_index,
           direction="inbound", text="msg"):
    store_communication(
        {
            "communication_id": comm_id,
            "source": "appfolio_communication_inbox_thread",
            "source_thread_id": thread_id,
            "conversation_identity_key": identity_key,
            "source_message_id": f"src-{comm_id}",
            "internal_user_id": "rachel-asst",
            "direction": direction,
            "message_text": text,
            "metadata": {
                "message_fingerprint": f"fp-{comm_id}" + "0" * 58,
                "rendered_index": rendered_index,
            },
        },
        db_path=db_path,
    )


class VerifiedThreadContextIdentityTests(unittest.TestCase):
    def test_loads_full_history_across_two_legacy_thread_ids_sharing_identity(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            identity_key = "af-native-thread-99"
            _store(
                db_path, comm_id="m0", thread_id="list-old-synthetic",
                identity_key=identity_key, rendered_index=0,
                text="first message",
            )
            _store(
                db_path, comm_id="m1", thread_id="af-native-thread-99",
                identity_key=identity_key, rendered_index=1,
                text="second message",
            )
            _mark_thread_read_verified(db_path, identity_key=identity_key)

            context = workflow.load_verified_thread_context(
                "af-native-thread-99", db_path=db_path,
                conversation_identity_key=identity_key,
            )
            self.assertEqual(context["message_count"], 2)
            self.assertEqual(
                {m["communication_id"] for m in context["messages"]},
                {"m0", "m1"},
            )

    def test_without_identity_key_only_matches_the_literal_thread_id(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            _store(
                db_path, comm_id="m0", thread_id="list-old-synthetic",
                identity_key="af-native-thread-99", rendered_index=0,
            )
            _mark_thread_read_verified(db_path, thread_id="af-native-thread-99")
            with self.assertRaises(workflow.CommunicationWorkflowError):
                workflow.load_verified_thread_context(
                    "af-native-thread-99", db_path=db_path,
                )


class DraftCandidateConsolidationTests(unittest.TestCase):
    def test_split_history_is_one_candidate_not_two(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            identity_key = "af-native-thread-55"
            _store(
                db_path, comm_id="a0", thread_id="list-legacy-a",
                identity_key=identity_key, rendered_index=0,
                direction="inbound", text="hello",
            )
            _store(
                db_path, comm_id="a1", thread_id="af-native-thread-55",
                identity_key=identity_key, rendered_index=1,
                direction="inbound", text="following up",
            )
            _mark_thread_read_verified(db_path, identity_key=identity_key)

            with patch.object(
                inbox, "ensure_appfolio_inbox_full_monitoring_scope",
            ), patch.object(
                inbox,
                "_default_response_draft_generator",
                lambda prompt: "Thanks, we'll follow up.",
            ):
                metrics = {}
                processed = inbox.process_pending_rachel_owned_response_drafts(
                    db_path=db_path, metrics=metrics,
                )
            self.assertEqual(metrics["candidate_threads"], 1)
            self.assertEqual(processed, 1)
            self.assertEqual(metrics["drafts_created"], 1)

            conn = sqlite3.connect(db_path)
            try:
                draft_count = conn.execute(
                    "SELECT COUNT(*) FROM communication_drafts"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(draft_count, 1)

    def test_rerun_after_consolidated_draft_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            identity_key = "af-native-thread-77"
            _store(
                db_path, comm_id="b0", thread_id="list-legacy-b",
                identity_key=identity_key, rendered_index=0,
                direction="inbound", text="hi",
            )
            _mark_thread_read_verified(db_path, identity_key=identity_key)

            def run():
                with patch.object(
                    inbox, "ensure_appfolio_inbox_full_monitoring_scope",
                ), patch.object(
                    inbox,
                    "_default_response_draft_generator",
                    lambda prompt: "Same draft text.",
                ):
                    metrics = {}
                    inbox.process_pending_rachel_owned_response_drafts(
                        db_path=db_path, metrics=metrics,
                    )
                    return metrics

            first = run()
            second = run()

        self.assertEqual(first["drafts_created"], 1)
        self.assertEqual(second["drafts_created"], 0)
        self.assertEqual(second["existing_drafts_skipped"], 1)

    def test_candidates_without_any_identity_key_still_processed(self):
        """Rows never backfilled (empty conversation_identity_key) must not
        be silently dropped - they fall back to grouping by legacy
        thread_id alone."""
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            initialize_event_memory(db_path)
            _store(
                db_path, comm_id="c0", thread_id="list-legacy-c",
                identity_key="", rendered_index=0,
                direction="inbound", text="no identity key yet",
            )
            _mark_thread_read_verified(db_path, thread_id="list-legacy-c")

            with patch.object(
                inbox, "ensure_appfolio_inbox_full_monitoring_scope",
            ), patch.object(
                inbox,
                "_default_response_draft_generator",
                lambda prompt: "Draft.",
            ):
                metrics = {}
                processed = inbox.process_pending_rachel_owned_response_drafts(
                    db_path=db_path, metrics=metrics,
                )
        self.assertEqual(metrics["candidate_threads"], 1)
        self.assertEqual(processed, 1)


if __name__ == "__main__":
    unittest.main()
