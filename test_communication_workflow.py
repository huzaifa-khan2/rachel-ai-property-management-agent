import json
import os
import sqlite3
import tempfile
import unittest

from rachel_communication_workflow import (
    CommunicationWorkflowError,
    build_response_prompt,
    classify_communication_ownership,
    create_response_draft_plan,
    generate_response_draft,
    load_verified_thread_context,
    store_response_draft,
)
from rachel_event_memory import (
    initialize_event_memory,
    record_communication_state,
    store_communication,
)

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

from rachel_appfolio_inbox import (
    process_pending_rachel_owned_response_drafts,
)


class CommunicationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "rachel.db")
        initialize_event_memory(self.db_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def _add_internal_user(
        self, user_id="casey-pm", display_name="Casey PM",
        portfolio_scope=None,
    ):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO internal_users (
                user_id, display_name, email, role, portfolio_scope,
                permission_scope, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, 'property_manager', ?,
                      '["property_management"]', 1, 'now', 'now')
            """,
            (
                user_id, display_name, f"{user_id}@example.com",
                json.dumps(portfolio_scope or ["100 Oak Street"]),
            ),
        )
        conn.commit()
        conn.close()

    def _assign_property(
        self, manager_user_id="casey-pm", property_key="100 Oak Street",
        property_group="Oak Portfolio",
    ):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO property_portfolio_assignments (
                property_key, property_group, manager_user_id,
                source, created_at, updated_at
            ) VALUES (?, ?, ?, 'test', 'now', 'now')
            """,
            (property_key, property_group, manager_user_id),
        )
        conn.commit()
        conn.close()

    def _store_verified_thread(
        self, messages, thread_id="thread-safe", internal_user_id=""
    ):
        for index, (direction, text) in enumerate(messages):
            store_communication(
                {
                    "source": "appfolio_communication_inbox_thread",
                    "source_thread_id": thread_id,
                    "source_message_id": f"message-{index}",
                    "direction": direction,
                    "message_text": text,
                    "internal_user_id": internal_user_id,
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
                "thread_id": thread_id,
                "source_message_id": "current-list-event",
                "preview": messages[-1][1],
                "timestamp": "now",
            },
            "thread_read_verified",
            db_path=self.db_path,
        )

    def test_pending_rachel_owned_thread_generates_full_history_draft(self):
        self._store_verified_thread(
            [
                ("outbound", "Earlier answer"),
                ("inbound", "Current question"),
            ],
            internal_user_id="rachel-asst",
        )
        prompts = []
        metrics = {}
        created = process_pending_rachel_owned_response_drafts(
            db_path=self.db_path,
            generator=lambda prompt: prompts.append(prompt) or "Safe draft",
            metrics=metrics,
        )
        self.assertEqual(created, 1)
        self.assertIn("Earlier answer", prompts[0])
        self.assertIn("Current question", prompts[0])
        self.assertEqual(metrics["drafts_created"], 1)
        self.assertEqual(metrics["composer_actions_attempted"], 0)
        self.assertEqual(metrics["send_actions_attempted"], 0)
        self.assertEqual(metrics["appfolio_writes_attempted"], 0)

    def test_pending_draft_restart_skips_generator_and_non_rachel_threads(self):
        self._store_verified_thread(
            [("inbound", "Rachel-owned question")],
            thread_id="thread-rachel",
            internal_user_id="rachel-asst",
        )
        self._store_verified_thread(
            [("inbound", "Human-owned question")],
            thread_id="thread-human",
            internal_user_id="casey-pm",
        )
        calls = []
        first = process_pending_rachel_owned_response_drafts(
            db_path=self.db_path,
            generator=lambda prompt: calls.append(prompt) or "Safe draft",
        )
        second_metrics = {}
        second = process_pending_rachel_owned_response_drafts(
            db_path=self.db_path,
            generator=lambda _prompt: self.fail(
                "an existing draft must skip the model call"
            ),
            metrics=second_metrics,
        )
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second_metrics["candidate_threads"], 1)
        self.assertEqual(second_metrics["existing_drafts_skipped"], 1)

    def test_pending_outbound_thread_is_non_actionable(self):
        self._store_verified_thread(
            [("inbound", "Question"), ("outbound", "Answered")],
            internal_user_id="rachel-asst",
        )
        metrics = {}
        created = process_pending_rachel_owned_response_drafts(
            db_path=self.db_path,
            generator=lambda _prompt: self.fail(
                "answered threads must not invoke the model"
            ),
            metrics=metrics,
        )
        self.assertEqual(created, 0)
        self.assertEqual(metrics["non_actionable_threads_skipped"], 1)

    def test_pending_draft_failure_retries_from_memory_without_browser(self):
        self._store_verified_thread(
            [("inbound", "Question")],
            internal_user_id="rachel-asst",
        )

        def fail_generator(_prompt):
            raise RuntimeError("transient model failure")

        with self.assertRaisesRegex(RuntimeError, "draft_failures=1"):
            metrics = {}
            process_pending_rachel_owned_response_drafts(
                db_path=self.db_path,
                generator=fail_generator,
                metrics=metrics,
            )
        self.assertEqual(
            metrics["draft_failure_reasons"],
            {"draft_generation_RuntimeError": 1},
        )
        created = process_pending_rachel_owned_response_drafts(
            db_path=self.db_path,
            generator=lambda _prompt: "Recovered draft",
        )
        self.assertEqual(created, 1)

    def test_rachel_owned_thread_is_eligible_for_automatic_reader(self):
        result = classify_communication_ownership(
            {
                "assigned_user": "Rachel Asst",
                "property": "100 Oak Street",
            },
            db_path=self.db_path,
        )
        self.assertEqual(result["classification"], "rachel_owned")
        self.assertEqual(result["owner_internal_user_id"], "rachel-asst")
        self.assertTrue(result["automatic_open_allowed"])

    def test_property_manager_is_resolved_without_manager_hardcoding(self):
        self._add_internal_user()
        result = classify_communication_ownership(
            {
                "assigned_user": "Casey PM",
                "property": "100 Oak Street",
            },
            db_path=self.db_path,
        )
        self.assertEqual(result["classification"], "pm_owned")
        self.assertEqual(result["owner_internal_user_id"], "casey-pm")
        self.assertTrue(result["portfolio_access_verified"])
        self.assertFalse(result["automatic_open_allowed"])

    def test_internal_owner_outside_portfolio_fails_closed(self):
        self._add_internal_user()
        result = classify_communication_ownership(
            {
                "assigned_user": "Casey PM",
                "property": "200 Pine Avenue",
            },
            db_path=self.db_path,
        )
        self.assertEqual(
            result["classification"], "internal_owner_scope_mismatch"
        )
        self.assertEqual(result["routing_action"], "manual_ownership_review")
        self.assertFalse(result["portfolio_access_verified"])

    def test_configured_portfolio_owner_routes_unassigned_row_without_open(self):
        self._add_internal_user(
            portfolio_scope=["Oak Portfolio"]
        )
        self._assign_property()
        result = classify_communication_ownership(
            {"assigned_user": "", "property": "100 Oak Street"},
            db_path=self.db_path,
        )
        self.assertEqual(
            result["classification"], "portfolio_manager_unassigned"
        )
        self.assertEqual(result["owner_internal_user_id"], "casey-pm")
        self.assertFalse(result["automatic_open_allowed"])

    def test_configured_portfolio_owner_mismatch_requires_manual_review(self):
        self._add_internal_user()
        self._add_internal_user(
            user_id="morgan-pm", display_name="Morgan PM",
            portfolio_scope=["Oak Portfolio"],
        )
        self._assign_property(manager_user_id="morgan-pm")
        result = classify_communication_ownership(
            {
                "assigned_user": "Casey PM",
                "property": "100 Oak Street",
            },
            db_path=self.db_path,
        )
        self.assertEqual(
            result["reason"], "configured_portfolio_owner_mismatch"
        )
        self.assertFalse(result["automatic_open_allowed"])

    def test_unknown_and_unresolved_human_ownership_never_auto_open(self):
        unknown = classify_communication_ownership(
            {"assigned_user": ""}, db_path=self.db_path
        )
        human = classify_communication_ownership(
            {"assigned_user": "External Person"}, db_path=self.db_path
        )
        self.assertEqual(unknown["classification"], "ownership_unknown")
        self.assertEqual(human["classification"], "human_owned")
        self.assertFalse(unknown["automatic_open_allowed"])
        self.assertFalse(human["automatic_open_allowed"])

    def test_verified_complete_history_is_ordered_and_used_in_prompt(self):
        self._store_verified_thread([
            ("inbound", "First question"),
            ("outbound", "First answer"),
            ("inbound", "Follow-up question"),
        ])
        ownership = {"classification": "rachel_owned"}
        plan = create_response_draft_plan(
            "thread-safe",
            ownership=ownership,
            authorization_reason="thread_owned_by_rachel",
            db_path=self.db_path,
        )
        prompt = build_response_prompt(plan)
        self.assertEqual(plan["message_count"], 3)
        self.assertEqual(plan["latest_direction"], "inbound")
        self.assertLess(prompt.index("First question"), prompt.index("First answer"))
        self.assertLess(prompt.index("First answer"), prompt.index("Follow-up"))
        self.assertTrue(plan["draft_only"])
        self.assertEqual(plan["composer_actions_attempted"], 0)
        self.assertEqual(plan["send_actions_attempted"], 0)

    def test_response_plan_rejects_unanswered_outbound(self):
        self._store_verified_thread([
            ("inbound", "Question"),
            ("outbound", "Already answered"),
        ])
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "no_unanswered_inbound_message"
        ):
            create_response_draft_plan(
                "thread-safe",
                ownership={"classification": "rachel_owned"},
                authorization_reason="thread_owned_by_rachel",
                db_path=self.db_path,
            )

    def test_response_plan_rejects_unverified_or_stale_history(self):
        self._store_verified_thread([("inbound", "Question")])
        record_communication_state(
            {
                "thread_id": "thread-safe",
                "source_message_id": "new-event",
                "preview": "Newer question",
                "timestamp": "later",
            },
            "list_observed_unopened",
            db_path=self.db_path,
        )
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "verified_full_thread_unavailable"
        ):
            load_verified_thread_context(
                "thread-safe", db_path=self.db_path
            )

    def test_verified_selector_survives_timestamp_only_list_reformat(self):
        self._store_verified_thread([("inbound", "Same question")])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO appfolio_thread_selector_state ("
            "selector_key, thread_id, latest_event_key, last_unread, "
            "processing_status, last_error, updated_at"
            ") VALUES ('selector', 'thread-safe', 'event-1', 0, "
            "'thread_read_verified', '', 'now')"
        )
        conn.commit()
        conn.close()
        record_communication_state(
            {
                "thread_id": "thread-safe",
                "source_message_id": "timestamp-only-event",
                "preview": "Same question",
                "timestamp": "Yesterday",
            },
            "list_observed_unopened",
            db_path=self.db_path,
        )
        context = load_verified_thread_context(
            "thread-safe", db_path=self.db_path
        )
        self.assertEqual(context["latest_direction"], "inbound")

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE appfolio_thread_selector_state "
            "SET processing_status = 'failed' WHERE selector_key = 'selector'"
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "verified_full_thread_unavailable"
        ):
            load_verified_thread_context(
                "thread-safe", db_path=self.db_path
            )

    def test_response_plan_rejects_missing_or_ambiguous_message_order(self):
        self._store_verified_thread([
            ("outbound", "Earlier"), ("inbound", "Later")
        ])
        conn = sqlite3.connect(self.db_path)
        metadata = json.loads(conn.execute(
            "SELECT metadata_json FROM communications "
            "WHERE source_message_id = 'message-1'"
        ).fetchone()[0])
        metadata.pop("rendered_index")
        conn.execute(
            "UPDATE communications SET metadata_json = ? "
            "WHERE source_message_id = 'message-1'",
            (json.dumps(metadata),),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "message_order_missing"
        ):
            load_verified_thread_context(
                "thread-safe", db_path=self.db_path
            )

    def test_response_plan_rejects_duplicate_or_missing_fingerprints(self):
        self._store_verified_thread([
            ("outbound", "Earlier"), ("inbound", "Later")
        ])
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT communication_id, metadata_json FROM communications "
            "ORDER BY source_message_id"
        ).fetchall()
        first_metadata = json.loads(rows[0][1])
        second_metadata = json.loads(rows[1][1])
        second_metadata["message_fingerprint"] = first_metadata[
            "message_fingerprint"
        ]
        conn.execute(
            "UPDATE communications SET metadata_json = ? "
            "WHERE communication_id = ?",
            (json.dumps(second_metadata), rows[1][0]),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "duplicate_message_fingerprint"
        ):
            load_verified_thread_context(
                "thread-safe", db_path=self.db_path
            )

    def test_non_rachel_owner_requires_valid_explicit_authority(self):
        self._store_verified_thread([("inbound", "Question")])
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "draft_authority_not_verified"
        ):
            create_response_draft_plan(
                "thread-safe",
                ownership={"classification": "pm_owned"},
                authorization_reason="",
                db_path=self.db_path,
            )
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "draft_authority_owner_mismatch"
        ):
            create_response_draft_plan(
                "thread-safe",
                ownership={"classification": "pm_owned"},
                authorization_reason="thread_owned_by_rachel",
                db_path=self.db_path,
            )

    def test_draft_storage_is_idempotent_restart_safe_and_write_free(self):
        self._store_verified_thread([("inbound", "Question")])
        plan = create_response_draft_plan(
            "thread-safe",
            ownership={"classification": "rachel_owned"},
            authorization_reason="thread_owned_by_rachel",
            db_path=self.db_path,
        )
        first = store_response_draft(
            plan, "Draft answer", db_path=self.db_path
        )
        second = store_response_draft(
            plan, "Draft answer", db_path=self.db_path
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(first["composer_actions_attempted"], 0)
        self.assertEqual(first["send_actions_attempted"], 0)
        conn = sqlite3.connect(self.db_path)
        count, status = conn.execute(
            "SELECT COUNT(*), MIN(status) FROM communication_drafts"
        ).fetchone()
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(status, "draft_only")

    def test_generation_uses_full_prompt_and_stale_context_cannot_store(self):
        self._store_verified_thread([
            ("outbound", "Earlier answer"),
            ("inbound", "Current question"),
        ])
        plan = create_response_draft_plan(
            "thread-safe",
            ownership={"classification": "rachel_owned"},
            authorization_reason="thread_owned_by_rachel",
            db_path=self.db_path,
        )
        prompts = []
        draft = generate_response_draft(
            plan,
            generator=lambda prompt: prompts.append(prompt) or "Draft answer",
        )
        self.assertEqual(draft, "Draft answer")
        self.assertIn("Earlier answer", prompts[0])
        self.assertIn("Current question", prompts[0])

        store_communication(
            {
                "source": "appfolio_communication_inbox_thread",
                "source_thread_id": "thread-safe",
                "source_message_id": "message-2",
                "direction": "inbound",
                "message_text": "New question arrived",
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
                "thread_id": "thread-safe",
                "source_message_id": "new-current-event",
                "preview": "New question arrived",
                "timestamp": "later",
            },
            "thread_read_verified",
            db_path=self.db_path,
        )
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "draft_context_changed_before_store"
        ):
            store_response_draft(
                plan, draft, db_path=self.db_path
            )

    def test_same_idempotency_key_cannot_overwrite_draft_text(self):
        self._store_verified_thread([("inbound", "Question")])
        plan = create_response_draft_plan(
            "thread-safe",
            ownership={"classification": "rachel_owned"},
            authorization_reason="thread_owned_by_rachel",
            db_path=self.db_path,
        )
        store_response_draft(plan, "First draft", db_path=self.db_path)
        with self.assertRaisesRegex(
            CommunicationWorkflowError, "draft_idempotency_conflict"
        ):
            store_response_draft(
                plan, "Different draft", db_path=self.db_path
            )


if __name__ == "__main__":
    unittest.main()
