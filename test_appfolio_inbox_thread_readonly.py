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
import rachel_appfolio_inbox_thread_dom_diagnostic as thread_dom_diagnostic
import rachel_appfolio_inbox_thread_readonly_qa as readonly_qa
import rachel_safety
from rachel_event_memory import inbox_event_key, inbox_selector_key


def list_row(**changes):
    row = {
        "thread_id": "list-thread-safe",
        "source_message_id": "list-event-safe",
        "href": "",
        "contact_name": "Jamie Tenant",
        "contact_role": "tenant",
        "list_detail": "Tenant · Oak 2",
        "assigned_user": "Client Owner",
        "property": "100 Oak Street",
        "unit": "2",
        "preview": "Please review the attached question",
        "timestamp": "August 12 10:00 AM",
        "unread": True,
    }
    row.update(changes)
    return row


class ScriptFrame:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def evaluate(self, script, *args):
        self.calls.append((script, args))
        value = self.results.get(script, {})
        return value() if callable(value) else value


class ReadOnlyInboxThreadTests(unittest.TestCase):
    def test_exact_one_row_targeting_by_selector_fingerprint(self):
        row = list_row()
        selected = inbox._target_row_for_selector_key(
            [list_row(preview="Other"), row], inbox_selector_key(row)
        )
        self.assertEqual(selected["contact_name"], row["contact_name"])
        self.assertEqual(selected["event_key"], inbox_event_key(row))
        self.assertEqual(selected["selector_key"], inbox_selector_key(row))

    def test_zero_target_matches_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            inbox._target_row_for_selector_key(
                [list_row()], "a" * 64
            )

    def test_duplicate_target_matches_fail_closed(self):
        row = list_row()
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            inbox._target_row_for_selector_key(
                [row, dict(row)], inbox_selector_key(row)
            )

    def test_global_rendered_row_uniqueness_is_required_before_click(self):
        row = list_row(event_key="b" * 64, selector_key="a" * 64)
        first = ScriptFrame({
            inbox._INBOX_EXACT_ROW_MATCH_SCRIPT: {"match_count": 1},
        })
        second = ScriptFrame({
            inbox._INBOX_EXACT_ROW_MATCH_SCRIPT: {"match_count": 1},
        })
        page = MagicMock(frames=[first, second])
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            inbox._click_exact_inbox_row(page, row)
        self.assertFalse(any(
            call[0] == inbox._INBOX_EXACT_ROW_CLICK_SCRIPT
            for frame in (first, second) for call in frame.calls
        ))

    def test_unread_human_owned_thread_is_blocked_before_click(self):
        row = list_row(selector_key=inbox_selector_key(list_row()))
        with self.assertRaisesRegex(PermissionError, "human_owned_unread"):
            inbox._authorize_inbox_thread_open(row)

    def test_unread_unknown_owner_thread_is_blocked_before_click(self):
        row = list_row(
            assigned_user="", selector_key=inbox_selector_key(list_row())
        )
        with self.assertRaisesRegex(PermissionError, "ownership_unknown_unread"):
            inbox._authorize_inbox_thread_open(row)

    def test_rachel_owned_thread_is_authorized(self):
        row = list_row(
            assigned_user="Rachel Asst",
            selector_key=inbox_selector_key(list_row()),
        )
        result = inbox._authorize_inbox_thread_open(row)
        self.assertEqual(result["reason"], "thread_owned_by_rachel")

    def test_exact_explicit_delegation_authorizes_human_owned_thread(self):
        selector_key = inbox_selector_key(list_row())
        row = list_row(selector_key=selector_key)
        result = inbox._authorize_inbox_thread_open(
            row,
            authorization={
                "kind": "explicit_delegation",
                "selector_key": selector_key,
                "delegated_by": "Client Owner",
                "authorization_id": "owner-controlled-vendor-test",
            },
        )
        self.assertEqual(result["reason"], "explicitly_delegated_to_rachel")

    def test_delegation_for_another_selector_fails_closed(self):
        row = list_row(selector_key=inbox_selector_key(list_row()))
        with self.assertRaisesRegex(PermissionError, "target mismatch"):
            inbox._authorize_inbox_thread_open(
                row,
                authorization={
                    "kind": "explicit_delegation",
                    "selector_key": "f" * 64,
                    "delegated_by": "Client Owner",
                    "authorization_id": "wrong-target",
                },
            )

    def test_human_owned_unread_operation_never_reaches_click(self):
        selected = list_row()
        selector_key = inbox_selector_key(selected)
        page = MagicMock()
        with patch.object(
            inbox, "human_delay", return_value=None
        ), patch.object(
            inbox,
            "ensure_appfolio_inbox_full_monitoring_scope",
            return_value={"verified_full_scope": True},
        ), patch.object(
            inbox, "_read_lightweight_inbox_rows", return_value=[selected]
        ), patch.object(inbox, "_click_exact_inbox_row") as click_row:
            with self.assertRaisesRegex(
                inbox.InboxThreadPipelineError,
                "authorization_verified: authorization_denied",
            ):
                inbox.inspect_one_appfolio_inbox_thread_read_only(
                    page,
                    target_selector_key=selector_key,
                    db_path=":memory:",
                )
        click_row.assert_not_called()

    def test_exact_rendered_row_click_records_sanitized_evidence(self):
        row = list_row(event_key="c" * 64, selector_key="a" * 64)
        frame = ScriptFrame({
            inbox._INBOX_EXACT_ROW_MATCH_SCRIPT: {"match_count": 1},
            inbox._INBOX_EXACT_ROW_CLICK_SCRIPT: {
                "match_count": 1, "clicked": True
            },
        })
        evidence = inbox._click_exact_inbox_row(
            MagicMock(frames=[frame]), row
        )
        self.assertEqual(evidence["selected_row_count"], 1)
        self.assertEqual(evidence["selected_selector_fingerprint"], "a" * 64)
        self.assertEqual(evidence["selected_event_fingerprint"], "c" * 64)
        self.assertEqual(
            [call[0] for call in frame.calls],
            [
                inbox._INBOX_EXACT_ROW_MATCH_SCRIPT,
                inbox._INBOX_EXACT_ROW_CLICK_SCRIPT,
            ],
        )

    def verified_thread(self, **changes):
        thread = {
            "thread_root_count": 1,
            "header_region_count": 1,
            "history_region_count": 1,
            "composer_region_count": 1,
            "history_wrapper_count": 1,
            "composer_textarea_count": 1,
            "send_control_count": 1,
            "send_via_sms_label_count": 1,
            "thread_history_present": True,
            "composer_present": True,
            "send_via_sms_present": True,
            "back_control_count": 1,
            "contact_candidate_count": 1,
            "role_candidate_count": 1,
            "contact_name": "Jamie Tenant",
            "contact_role": "tenant",
            "detail_context": "100 Oak Street Unit 2",
            "message_structure_valid": True,
            "rendered_message_item_count": 1,
            "invalid_message_structure_count": 0,
            "message_count": 1,
            "headings": ["Jamie Tenant"],
            "context_fields": ["Tenant", "100 Oak Street", "Unit 2"],
            "messages": [{
                "direction": "inbound",
                "timestamp": "August 12 10:00 AM",
                "message_text": "Please review the attached question",
            }],
        }
        thread.update(changes)
        return thread

    def test_opened_thread_identity_and_context_are_verified(self):
        frame = ScriptFrame({
            inbox._INBOX_THREAD_READY_SCRIPT: {
                "ready_thread_root_count": 1,
                "thread_ready": True,
                "rendered_message_item_count": 1,
            },
            inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread()
        })
        result = inbox._read_verified_thread(
            MagicMock(frames=[frame]), list_row()
        )
        self.assertGreaterEqual(result["identity_fields_matched"], 1)
        self.assertIs(result["verified_frame"], frame)

    def test_opened_thread_identity_mismatch_fails_closed(self):
        frame = ScriptFrame({
            inbox._INBOX_THREAD_READY_SCRIPT: {
                "ready_thread_root_count": 1,
                "thread_ready": True,
                "rendered_message_item_count": 1,
            },
            inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(
                contact_name="Different Contact",
            )
        })
        with self.assertRaisesRegex(
            RuntimeError, "contact_identity_mismatch"
        ):
            inbox._read_verified_thread(
                MagicMock(frames=[frame]), list_row()
            )

    def test_history_wrapper_count_is_diagnostic_not_a_blocker(self):
        react_rewrapped = self.verified_thread(
            history_wrapper_count=2
        )
        frame = ScriptFrame({
            inbox._INBOX_THREAD_READY_SCRIPT: {
                "ready_thread_root_count": 1,
                "thread_ready": True,
                "rendered_message_item_count": 1,
            },
            inbox._INBOX_THREAD_READ_SCRIPT: react_rewrapped
        })
        result = inbox._read_verified_thread(
            MagicMock(frames=[frame]), list_row()
        )
        self.assertEqual(result["history_wrapper_count"], 2)
        self.assertEqual(
            inbox._THREAD_ASSERTION_CLASSIFICATIONS["history_wrapper_count"],
            "diagnostic_only",
        )

    def test_opened_thread_role_mismatch_fails_closed(self):
        frame = ScriptFrame({
            inbox._INBOX_THREAD_READY_SCRIPT: {
                "ready_thread_root_count": 1,
                "thread_ready": True,
                "rendered_message_item_count": 1,
            },
            inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(
                contact_role="owner"
            )
        })
        with self.assertRaisesRegex(RuntimeError, "contact_role_mismatch"):
            inbox._read_verified_thread(
                MagicMock(frames=[frame]), list_row()
            )

    def test_parser_uses_captured_appfolio_light_dom_signatures(self):
        script = inbox._INBOX_THREAD_READ_SCRIPT
        for evidence in (
            "child.getAttribute('role') === 'list'",
            "querySelectorAll('[role=\"listitem\"]')",
            "item.classList.contains('text-start')",
            "item.classList.contains('text-end')",
            "'d-inline-block', 'text-start', 'rounded-3'",
            "directChildren(child, isTextBubble).length === 1",
            "'py-2', 'px-3', 'border-0', 'text-muted'",
            "Array.from(child.querySelectorAll('textarea'))",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, script)

    def test_thread_parser_runs_only_after_structure_is_ready(self):
        class DelayedFrame(ScriptFrame):
            def __init__(self, thread):
                super().__init__({inbox._INBOX_THREAD_READ_SCRIPT: thread})
                self.readiness_checks = 0

            def evaluate(self, script, *args):
                self.calls.append((script, args))
                if script == inbox._INBOX_THREAD_READY_SCRIPT:
                    self.readiness_checks += 1
                    if self.readiness_checks < 3:
                        return {
                            "ready_thread_root_count": 0,
                            "thread_ready": False,
                            "rendered_message_item_count": 0,
                        }
                    return {
                        "ready_thread_root_count": 1,
                        "thread_ready": True,
                        "rendered_message_item_count": 1,
                    }
                return self.results.get(script, {})

        frame = DelayedFrame(self.verified_thread())
        page = MagicMock(frames=[frame])
        result = inbox._read_verified_thread(page, list_row())
        scripts = [call[0] for call in frame.calls]
        self.assertEqual(result["readiness_attempts"], 3)
        self.assertEqual(scripts.count(inbox._INBOX_THREAD_READ_SCRIPT), 1)
        self.assertGreater(
            scripts.index(inbox._INBOX_THREAD_READ_SCRIPT),
            scripts.index(inbox._INBOX_THREAD_READY_SCRIPT),
        )
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_parser_retries_a_replaced_frame_evaluation(self):
        class ReplacedFrame(ScriptFrame):
            def __init__(self, thread):
                super().__init__({})
                self.thread = thread
                self.parser_attempts = 0

            def evaluate(self, script, *args):
                self.calls.append((script, args))
                if script == inbox._INBOX_THREAD_READY_SCRIPT:
                    return {
                        "ready_thread_root_count": 1,
                        "thread_ready": True,
                        "rendered_message_item_count": 1,
                    }
                if script == inbox._INBOX_THREAD_READ_SCRIPT:
                    self.parser_attempts += 1
                    if self.parser_attempts == 1:
                        raise RuntimeError("detached React frame")
                    return self.thread
                return {}

        frame = ReplacedFrame(self.verified_thread())
        page = MagicMock(frames=[frame])
        result = inbox._read_verified_thread(page, list_row())
        self.assertEqual(result["readiness_attempts"], 2)
        self.assertEqual(frame.parser_attempts, 2)

    def test_multiple_ready_thread_roots_fail_before_parsing(self):
        frame = ScriptFrame({
            inbox._INBOX_THREAD_READY_SCRIPT: {
                "ready_thread_root_count": 2,
                "thread_ready": False,
                "rendered_message_item_count": 0,
            },
            inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(),
        })
        with self.assertRaisesRegex(RuntimeError, "ambiguous_thread_roots"):
            inbox._read_verified_thread(
                MagicMock(frames=[frame]), list_row()
            )
        self.assertFalse(any(
            call[0] == inbox._INBOX_THREAD_READ_SCRIPT
            for call in frame.calls
        ))

    def test_malformed_message_reports_exact_sanitized_reason(self):
        malformed = self.verified_thread(
            message_structure_valid=False,
            invalid_message_structure_count=1,
            message_count=0,
            messages=[],
            message_failure_counts={"direction_ambiguous": 1},
        )
        frame = ScriptFrame({
            inbox._INBOX_THREAD_READY_SCRIPT: {
                "ready_thread_root_count": 1,
                "thread_ready": True,
                "rendered_message_item_count": 1,
            },
            inbox._INBOX_THREAD_READ_SCRIPT: malformed,
        })
        page = MagicMock(frames=[frame])
        with self.assertRaisesRegex(
            inbox.InboxThreadPipelineError,
            "message_direction_ambiguous",
        ):
            inbox._read_verified_thread(page, list_row())
        self.assertEqual(page.wait_for_timeout.call_count, 19)

    def test_semantic_postcheck_accepts_replaced_dom_and_timestamp_change(self):
        selected = list_row()
        original = self.verified_thread()
        original["conversation_route_fingerprint"] = inbox.hashlib.sha256(
            "/texting/inbox/conversation-safe".encode("utf-8")
        ).hexdigest()
        original_messages = inbox._normalize_thread_messages(
            selected["thread_id"], original["messages"]
        )
        rerendered = self.verified_thread(messages=[{
            "direction": "inbound",
            "timestamp": "Yesterday",
            "message_text": "Please review the attached question",
        }])
        replacement_frame = ScriptFrame({
            inbox._INBOX_THREAD_READ_SCRIPT: rerendered,
        })
        page = MagicMock(frames=[replacement_frame])
        page.url = (
            "https://example.appfolio.com/texting/inbox/conversation-safe"
        )
        trace = []
        result = inbox._semantic_thread_postcheck(
            page, selected, original, original_messages, stage_trace=trace
        )
        self.assertEqual(result["contact_name"], "Jamie Tenant")
        self.assertEqual(trace[-1]["stage"], "semantic_postcheck_passed")
        self.assertEqual(trace[-1]["status"], "passed")

    def test_semantic_postcheck_blocks_true_conversation_change(self):
        selected = list_row()
        original = self.verified_thread()
        page = MagicMock(frames=[ScriptFrame({
            inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(
                contact_name="Different Contact"
            )
        })])
        page.url = "https://example.appfolio.com/texting/inbox/same-route"
        original["conversation_route_fingerprint"] = (
            inbox._thread_route_fingerprint(page)
        )
        normalized = inbox._normalize_thread_messages(
            selected["thread_id"], original["messages"]
        )
        with self.assertRaisesRegex(
            inbox.InboxThreadPipelineError, "contact_identity_mismatch"
        ):
            inbox._semantic_thread_postcheck(
                page, selected, original, normalized
            )

    def test_semantic_postcheck_retries_disappearing_history_then_fails(self):
        selected = list_row()
        original = self.verified_thread()
        missing = self.verified_thread(
            thread_root_count=0,
            header_region_count=0,
            history_region_count=0,
            composer_region_count=0,
            message_structure_valid=False,
            rendered_message_item_count=0,
            message_count=0,
            messages=[],
            structure_failure_reasons=[
                "thread_root_absent", "history_region_absent"
            ],
        )
        page = MagicMock(frames=[ScriptFrame({
            inbox._INBOX_THREAD_READ_SCRIPT: missing,
        })])
        page.url = "https://example.appfolio.com/texting/inbox/same-route"
        original["conversation_route_fingerprint"] = (
            inbox._thread_route_fingerprint(page)
        )
        normalized = inbox._normalize_thread_messages(
            selected["thread_id"], original["messages"]
        )
        with self.assertRaisesRegex(
            inbox.InboxThreadPipelineError,
            "semantic_postcheck_timeout.*history_region_absent",
        ):
            inbox._semantic_thread_postcheck(
                page, selected, original, normalized
            )
        self.assertEqual(page.wait_for_timeout.call_count, 11)

    def test_timestamp_reformatting_does_not_change_fingerprint(self):
        before = inbox._normalize_thread_messages("thread-1", [{
            "direction": "inbound",
            "timestamp": "August 12 10:00 AM",
            "message_text": "Same body",
        }])
        after = inbox._normalize_thread_messages("thread-1", [{
            "direction": "inbound",
            "timestamp": "Yesterday",
            "message_text": "Same body",
        }])
        self.assertEqual(
            before[0]["message_fingerprint"],
            after[0]["message_fingerprint"],
        )

    def test_identical_messages_get_distinct_stable_occurrence_fingerprints(self):
        messages = [
            {
                "direction": "inbound",
                "timestamp": "10:00",
                "message_text": "Repeated body",
            },
            {
                "direction": "inbound",
                "timestamp": "10:01",
                "message_text": "Repeated body",
            },
        ]
        first = inbox._normalize_thread_messages("thread-1", messages)
        second = inbox._normalize_thread_messages("thread-1", messages)
        first_keys = [item["message_fingerprint"] for item in first]
        self.assertEqual(
            first_keys,
            [item["message_fingerprint"] for item in second],
        )
        self.assertEqual(len(set(first_keys)), 2)

    def test_semantic_postcheck_blocks_conversation_route_change(self):
        selected = list_row()
        original = self.verified_thread()
        original["conversation_route_fingerprint"] = inbox.hashlib.sha256(
            "/texting/inbox/original".encode("utf-8")
        ).hexdigest()
        normalized = inbox._normalize_thread_messages(
            selected["thread_id"], original["messages"]
        )
        page = MagicMock(frames=[ScriptFrame({
            inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(),
        })])
        page.url = "https://example.appfolio.com/texting/inbox/different"
        with self.assertRaisesRegex(
            inbox.InboxThreadPipelineError, "conversation_route_changed"
        ):
            inbox._semantic_thread_postcheck(
                page, selected, original, normalized
            )

    def test_semantic_postcheck_blocks_ambiguous_replacement_frames(self):
        selected = list_row()
        original = self.verified_thread()
        page = MagicMock(frames=[
            ScriptFrame({
                inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(),
            }),
            ScriptFrame({
                inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(),
            }),
        ])
        page.url = "https://example.appfolio.com/texting/inbox/same-route"
        original["conversation_route_fingerprint"] = (
            inbox._thread_route_fingerprint(page)
        )
        normalized = inbox._normalize_thread_messages(
            selected["thread_id"], original["messages"]
        )
        with self.assertRaisesRegex(
            inbox.InboxThreadPipelineError,
            "ambiguous_semantic_thread_states",
        ):
            inbox._semantic_thread_postcheck(
                page, selected, original, normalized
            )

    def test_non_message_history_nodes_are_diagnostic_only(self):
        result = self.verified_thread(non_message_history_node_count=3)
        self.assertEqual(inbox._thread_parse_failure_reasons(result), [])
        self.assertEqual(
            inbox._THREAD_ASSERTION_CLASSIFICATIONS[
                "non_message_history_nodes"
            ],
            "diagnostic_only",
        )

    def test_opened_thread_diagnostic_reports_legacy_selector_miss(self):
        thread_frame = ScriptFrame({
            inbox._INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT: {
                "schema_version": 1,
                "sanitized": True,
                "legacy_predicate_would_accept": False,
                "legacy_selector_counts": {
                    "history_candidates": 0,
                    "composer_candidates": 0,
                    "send_via_sms_labels": 1,
                    "back_to_inbox_controls": 1,
                },
                "repeated_structure_groups": [],
                "scroll_containers": [],
            }
        })
        other_frame = ScriptFrame({
            inbox._INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT: {
                "schema_version": 1,
                "sanitized": True,
                "legacy_predicate_would_accept": False,
                "legacy_selector_counts": {
                    "history_candidates": 0,
                    "composer_candidates": 0,
                    "send_via_sms_labels": 0,
                    "back_to_inbox_controls": 0,
                },
            }
        })
        report, return_frame = (
            inbox.diagnose_opened_appfolio_inbox_thread_structure(
                MagicMock(frames=[other_frame, thread_frame])
            )
        )
        self.assertEqual(report["legacy_verified_frame_count"], 0)
        self.assertEqual(report["back_to_inbox_control_count"], 1)
        self.assertIs(return_frame, thread_frame)

    def test_opened_thread_diagnostic_requires_unique_back_control(self):
        structure = {
            "schema_version": 1,
            "sanitized": True,
            "legacy_predicate_would_accept": False,
            "legacy_selector_counts": {
                "back_to_inbox_controls": 1,
            },
        }
        frames = [
            ScriptFrame({
                inbox._INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT: structure
            }),
            ScriptFrame({
                inbox._INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT: structure
            }),
        ]
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            inbox.diagnose_opened_appfolio_inbox_thread_structure(
                MagicMock(frames=frames)
            )

    def test_opened_thread_diagnostic_validator_blocks_raw_content(self):
        with self.assertRaisesRegex(ValueError, "unsafe DOM diagnostic"):
            inbox._validate_sanitized_dom_diagnostic({
                "safe_structure": {"message_text": "must not escape"}
            })

    def test_opened_thread_diagnostic_evaluation_is_guard_allowlisted(self):
        state = readonly_qa._ReadOnlyGuardState()
        raw_frame = ScriptFrame({
            inbox._INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT: {
                "sanitized": True
            }
        })
        guarded = readonly_qa._ReadOnlyThreadFrame(raw_frame, state)
        self.assertEqual(
            guarded.evaluate(
                inbox._INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT
            ),
            {"sanitized": True},
        )
        self.assertEqual(state.blocked_actions, 0)

    def test_opened_thread_diagnostic_gate_reuses_readonly_authorization(self):
        with patch.object(
            thread_dom_diagnostic.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY",
            "d" * 64,
        ), patch.object(
            thread_dom_diagnostic.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_EXPECTED_EVENT_KEY",
            "e" * 64,
        ), patch.object(
            thread_dom_diagnostic.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_DELEGATED_BY",
            "Client Owner",
        ), patch.object(
            thread_dom_diagnostic.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_AUTHORIZATION_ID",
            "owner-controlled-vendor-test",
        ), patch.object(
            thread_dom_diagnostic.rachel_safety,
            "appfolio_readonly_thread_qa_allowed",
            return_value=(True, "allowed"),
        ) as allowed:
            self.assertEqual(
                thread_dom_diagnostic._authorization(),
                (
                    "d" * 64,
                    "e" * 64,
                    "Client Owner",
                    "owner-controlled-vendor-test",
                ),
            )
        allowed.assert_called_once_with(
            "d" * 64,
            "Client Owner",
            "owner-controlled-vendor-test",
        )

    def test_multiple_messages_get_stable_individual_fingerprints(self):
        messages = [
            {"direction": "inbound", "timestamp": "10:00", "message_text": "One"},
            {"direction": "outbound", "timestamp": "10:01", "message_text": "Two"},
        ]
        first = inbox._normalize_thread_messages("thread-1", messages)
        second = inbox._normalize_thread_messages("thread-1", messages)
        self.assertEqual(len(first), 2)
        self.assertEqual(
            [item["message_fingerprint"] for item in first],
            [item["message_fingerprint"] for item in second],
        )
        self.assertNotEqual(
            first[0]["message_fingerprint"],
            first[1]["message_fingerprint"],
        )

    def test_selector_identity_ignores_timestamp_and_read_state(self):
        original = list_row(timestamp="7:23 PM", unread=True)
        reformatted = list_row(timestamp="Yesterday", unread=False)
        self.assertEqual(
            inbox_selector_key(original), inbox_selector_key(reformatted)
        )
        self.assertNotEqual(
            inbox_event_key(original), inbox_event_key(reformatted)
        )

    def test_message_storage_is_deduplicated_and_non_wo_is_valid(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            thread = self.verified_thread(messages=[
                {
                    "direction": "inbound",
                    "timestamp": "10:00",
                    "message_text": "A general property question",
                },
                {
                    "direction": "outbound",
                    "timestamp": "10:01",
                    "message_text": "Thank you for the update",
                },
            ])
            row = list_row(
                event_key=inbox_event_key(list_row()),
                selector_key=inbox_selector_key(list_row()),
            )
            first = inbox._store_verified_thread(row, thread, db_path=db_path)
            second = inbox._store_verified_thread(row, thread, db_path=db_path)
            self.assertEqual(first["messages_stored"], 2)
            self.assertEqual(second["messages_stored"], 2)
            self.assertEqual(first["messages_fingerprinted"], 2)
            self.assertEqual(first["unique_message_fingerprints"], 2)
            self.assertEqual(first["messages_newly_stored"], 2)
            self.assertEqual(first["messages_previously_stored"], 0)
            self.assertEqual(second["messages_newly_stored"], 0)
            self.assertEqual(second["messages_previously_stored"], 2)
            self.assertTrue(first["message_deduplication_verified"])
            self.assertTrue(second["message_deduplication_verified"])
            self.assertEqual(second["durable_message_rows_after"], 2)
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT related_wo, processing_status FROM communications"
            ).fetchall()
            conn.close()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row[0] for row in rows}, {""})
            self.assertEqual(
                {row[1] for row in rows}, {"thread_read_verified"}
            )

    def test_verified_rachel_owned_history_retains_draft_routing_identity(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            row = list_row(assigned_user="Rachel Asst")
            row.update({
                "event_key": inbox_event_key(row),
                "selector_key": inbox_selector_key(row),
            })
            inbox._store_verified_thread(
                row,
                self.verified_thread(messages=[{
                    "direction": "inbound",
                    "timestamp": "10:00",
                    "message_text": "A safe question",
                }]),
                db_path=db_path,
            )
            conn = sqlite3.connect(db_path)
            internal_user_id, metadata_json = conn.execute(
                "SELECT internal_user_id, metadata_json FROM communications "
                "WHERE source = 'appfolio_communication_inbox_thread'"
            ).fetchone()
            conn.close()
            self.assertEqual(internal_user_id, "rachel-asst")
            self.assertIn(
                '"ownership_classification": "rachel_owned"', metadata_json
            )

    def test_explicit_wo_text_stores_when_optional_wo_tables_are_absent(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "event-memory-only.db")
            thread = self.verified_thread(messages=[{
                "direction": "inbound",
                "timestamp": "10:00",
                "message_text": "Question about WO 1234-5",
            }])
            row = list_row(
                event_key=inbox_event_key(list_row()),
                selector_key=inbox_selector_key(list_row()),
            )
            result = inbox._store_verified_thread(
                row, thread, db_path=db_path
            )
            self.assertEqual(result["messages_stored"], 1)
            self.assertEqual(result["explicit_wo_relationships"], 0)
            self.assertTrue(result["message_deduplication_verified"])

    def test_return_uses_exact_back_control_and_verifies_inbox_url(self):
        frame = ScriptFrame({
            inbox._INBOX_BACK_TO_INBOX_COUNT_SCRIPT: {
                "back_control_count": 1
            },
            inbox._INBOX_BACK_TO_INBOX_SCRIPT: {
                "clicked": True, "match_count": 1
            }
        })
        page = MagicMock()
        page.frames = [frame]
        page.url = "https://example.appfolio.com/texting/inbox"
        inbox._return_to_inbox(page, {"verified_frame": frame})
        self.assertEqual(
            [call[0] for call in frame.calls],
            [
                inbox._INBOX_BACK_TO_INBOX_COUNT_SCRIPT,
                inbox._INBOX_BACK_TO_INBOX_SCRIPT,
            ],
        )

    def test_return_fails_closed_for_ambiguous_back_controls(self):
        page = MagicMock(frames=[
            ScriptFrame({
                inbox._INBOX_BACK_TO_INBOX_COUNT_SCRIPT: {
                    "back_control_count": 1
                },
            }),
            ScriptFrame({
                inbox._INBOX_BACK_TO_INBOX_COUNT_SCRIPT: {
                    "back_control_count": 1
                },
            }),
        ])
        with self.assertRaisesRegex(
            inbox.InboxThreadPipelineError,
            "ambiguous_back_controls",
        ):
            inbox._return_to_inbox(page)

    def test_return_retries_replaced_or_delayed_back_control(self):
        page = MagicMock()
        page.url = "https://example.appfolio.com/texting/inbox/thread"

        class DelayedBackFrame(ScriptFrame):
            def __init__(self):
                super().__init__({})
                self.count_attempts = 0

            def evaluate(self, script, *args):
                self.calls.append((script, args))
                if script == inbox._INBOX_BACK_TO_INBOX_COUNT_SCRIPT:
                    self.count_attempts += 1
                    return {
                        "back_control_count": int(self.count_attempts >= 3)
                    }
                if script == inbox._INBOX_BACK_TO_INBOX_SCRIPT:
                    page.url = "https://example.appfolio.com/texting/inbox"
                    return {"clicked": True, "match_count": 1}
                return {}

        frame = DelayedBackFrame()
        page.frames = [frame]
        attempts = inbox._return_to_inbox(page)
        self.assertEqual(attempts, 1)
        self.assertEqual(frame.count_attempts, 3)
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_read_operation_detects_unread_to_read_without_toggling(self):
        selected = list_row()
        selected_key = inbox_selector_key(selected)
        returned = list_row(unread=False)
        page = MagicMock()
        page.url = "https://example.appfolio.com/texting/inbox"

        def page_evaluate_side_effect(script, *args):
            if script == inbox._INBOX_SEMANTIC_RETURN_VERIFICATION_SCRIPT:
                return {
                    "list_root_present": True, "row_count": 1,
                    "composer_active": False, "thread_detail_active": False,
                }
            if script == inbox._INBOX_FILTER_SCOPE_RESET_SCRIPT:
                return {
                    "status": "full_scope_verified",
                    "verified_full_scope": True,
                    "conversation_rows_before": 1,
                    "conversation_rows_after": 1,
                }
            return {}

        page.evaluate = MagicMock(side_effect=page_evaluate_side_effect)
        thread = self.verified_thread()
        thread["verified_frame"] = ScriptFrame({})
        with patch.object(
            inbox, "human_delay", return_value=None
        ), patch.object(
            inbox,
            "ensure_appfolio_inbox_full_monitoring_scope",
            return_value={"verified_full_scope": True},
        ), patch.object(
            inbox,
            "_read_lightweight_inbox_rows",
            side_effect=[[selected], [returned]],
        ), patch.object(
            inbox,
            "_click_exact_inbox_row",
            return_value={
                "selected_row_count": 1,
                "selected_event_fingerprint": selected_key,
                "selected_thread_fingerprint": "f" * 64,
            },
        ), patch.object(
            inbox, "_read_verified_thread", return_value=thread
        ), patch.object(
            inbox,
            "_store_verified_thread",
            return_value={
                "messages_parsed": 1,
                "messages_stored": 1,
                "messages_fingerprinted": 1,
                "unique_message_fingerprints": 1,
                "messages_previously_stored": 0,
                "messages_newly_stored": 1,
                "durable_message_rows_after": 1,
                "message_deduplication_verified": True,
                "explicit_wo_relationships": 0,
            },
        ), patch.object(
            inbox, "_semantic_thread_postcheck", return_value={}
        ), patch.object(inbox, "_return_to_inbox", return_value=1):
            metrics = inbox.inspect_one_appfolio_inbox_thread_read_only(
                page,
                target_selector_key=selected_key,
                expected_event_key=inbox_event_key(selected),
                authorization={
                    "kind": "explicit_delegation",
                    "selector_key": selected_key,
                    "delegated_by": "Client Owner",
                    "authorization_id": "owner-approved-test",
                },
                db_path=":memory:",
            )
        self.assertTrue(metrics["unread_before"])
        self.assertFalse(metrics["unread_after"])
        self.assertTrue(metrics["unread_to_read_transition"])
        self.assertEqual(metrics["read_state_toggles_attempted"], 0)
        self.assertEqual(metrics["outbound_messages_attempted"], 0)
        self.assertEqual(metrics["composer_inputs_attempted"], 0)

    def test_complete_pipeline_emits_ordered_sanitized_stage_trace(self):
        selected = list_row()
        selected_key = inbox_selector_key(selected)
        page = MagicMock()
        page.url = "https://example.appfolio.com/texting/inbox"

        class PipelineFrame(ScriptFrame):
            def evaluate(self, script, *args):
                self.calls.append((script, args))
                if script == inbox._INBOX_BACK_TO_INBOX_SCRIPT:
                    page.url = "https://example.appfolio.com/texting/inbox"
                value = self.results.get(script, {})
                return value() if callable(value) else value

        frame = PipelineFrame({
            inbox._INBOX_THREAD_READY_SCRIPT: {
                "ready_thread_root_count": 1,
                "thread_ready": True,
                "rendered_message_item_count": 1,
            },
            inbox._INBOX_THREAD_READ_SCRIPT: self.verified_thread(),
            inbox._INBOX_BACK_TO_INBOX_COUNT_SCRIPT: {
                "back_control_count": 1,
            },
            inbox._INBOX_BACK_TO_INBOX_SCRIPT: {
                "clicked": True,
                "match_count": 1,
            },
            inbox._INBOX_SEMANTIC_RETURN_VERIFICATION_SCRIPT: {
                "list_root_present": True,
                "row_count": 1,
                "composer_active": False,
                "thread_detail_active": False,
            },
        })
        page.frames = [frame]
        page.evaluate = MagicMock(return_value={
            "status": "full_scope_verified",
            "verified_full_scope": True,
            "conversation_rows_before": 1,
            "conversation_rows_after": 1,
        })

        def exact_click(_page, _selected):
            page.url = (
                "https://example.appfolio.com/texting/inbox/"
                "conversation-safe"
            )
            return {
                "selected_row_count": 1,
                "selected_event_fingerprint": "e" * 64,
                "selected_thread_fingerprint": "f" * 64,
            }

        with tempfile.TemporaryDirectory() as root, patch.object(
            inbox, "human_delay", return_value=None
        ), patch.object(
            inbox,
            "ensure_appfolio_inbox_full_monitoring_scope",
            return_value={"verified_full_scope": True},
        ), patch.object(
            inbox,
            "_read_lightweight_inbox_rows",
            side_effect=[[selected], [list_row(unread=False)]],
        ), patch.object(
            inbox, "_click_exact_inbox_row", side_effect=exact_click
        ):
            metrics = inbox.inspect_one_appfolio_inbox_thread_read_only(
                page,
                target_selector_key=selected_key,
                authorization={
                    "kind": "explicit_delegation",
                    "selector_key": selected_key,
                    "delegated_by": "Client Owner",
                    "authorization_id": "owner-approved-test",
                },
                db_path=os.path.join(root, "rachel.db"),
            )
        expected = [
            "target_resolved", "authorization_verified", "thread_clicked",
            "thread_ready", "identity_verified", "history_verified",
            "messages_parsed", "fingerprints_created",
            "durable_upsert_complete", "semantic_postcheck_passed",
            "returned_to_inbox",
        ]
        self.assertEqual(
            [entry["stage"] for entry in metrics["stage_trace"]],
            expected,
        )
        self.assertTrue(all(
            entry["status"] == "passed"
            for entry in metrics["stage_trace"]
        ))
        self.assertEqual(metrics["appfolio_writes_attempted"], 0)
        self.assertEqual(metrics["outbound_messages_attempted"], 0)
        self.assertEqual(metrics["composer_inputs_attempted"], 0)
        self.assertTrue(metrics["returned_to_inbox"])

    def test_verification_failure_still_returns_to_inbox(self):
        selected = list_row()
        selected_key = inbox_selector_key(selected)
        page = MagicMock()
        with patch.object(
            inbox, "human_delay", return_value=None
        ), patch.object(
            inbox,
            "ensure_appfolio_inbox_full_monitoring_scope",
            return_value={"verified_full_scope": True},
        ), patch.object(
            inbox, "_read_lightweight_inbox_rows", return_value=[selected]
        ), patch.object(
            inbox,
            "_click_exact_inbox_row",
            return_value={"selected_row_count": 1},
        ), patch.object(
            inbox,
            "_read_verified_thread",
            side_effect=inbox.InboxThreadPipelineError(
                "identity_verified", "contact_identity_mismatch"
            ),
        ), patch.object(inbox, "_return_to_inbox") as return_to_inbox:
            return_to_inbox.return_value = 1
            with self.assertRaisesRegex(
                RuntimeError, "contact_identity_mismatch"
            ):
                inbox.inspect_one_appfolio_inbox_thread_read_only(
                    page,
                    target_selector_key=selected_key,
                    authorization={
                        "kind": "explicit_delegation",
                        "selector_key": selected_key,
                        "delegated_by": "Client Owner",
                        "authorization_id": "owner-approved-test",
                    },
                    db_path=":memory:",
                )
        return_to_inbox.assert_called_once_with(page, {})

    def test_guard_blocks_typing_sending_and_generic_clicks(self):
        state = readonly_qa._ReadOnlyGuardState()
        guarded = readonly_qa._ReadOnlyThreadPage(MagicMock(), state)
        for action in ("click", "fill", "type", "press", "get_by_role"):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                getattr(guarded, action)("anything")
        self.assertEqual(state.blocked_actions, 5)

    def test_qa_reuses_one_manager_page_and_context(self):
        page = MagicMock()
        page.context = object()
        manager = MagicMock()
        manager.operation.return_value.__enter__.return_value = page
        manager.metrics = {"context_starts": 1}
        metrics = {
            "rows_seen": 5,
            "selected_row_count": 1,
            "conversation_opens": 1,
            "identity_fields_compared": 2,
            "identity_fields_matched": 2,
            "messages_parsed": 2,
            "messages_stored": 2,
            "messages_fingerprinted": 2,
            "unique_message_fingerprints": 2,
            "messages_previously_stored": 0,
            "messages_newly_stored": 2,
            "durable_message_rows_after": 2,
            "message_deduplication_verified": True,
            "rendered_message_item_count": 2,
            "invalid_message_structure_count": 0,
            "readiness_attempts": 1,
            "filter_scope_reverified": True,
            "returned_to_inbox": True,
            "opening_treated_as_global_read_mutation": True,
            "open_authorization_reason": "explicitly_delegated_to_rachel",
            "stage_trace": [
                {"stage": stage, "status": "passed", "reason": "test"}
                for stage in (
                    "target_resolved", "authorization_verified",
                    "thread_clicked", "thread_ready", "identity_verified",
                    "history_verified", "messages_parsed",
                    "fingerprints_created", "durable_upsert_complete",
                    "semantic_postcheck_passed", "returned_to_inbox",
                )
            ],
        }
        with patch.object(
            readonly_qa.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY",
            "d" * 64,
        ), patch.object(
            readonly_qa.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_EXPECTED_EVENT_KEY",
            "e" * 64,
        ), patch.object(
            readonly_qa.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_DELEGATED_BY",
            "Client Owner",
        ), patch.object(
            readonly_qa.rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_AUTHORIZATION_ID",
            "owner-controlled-vendor-test",
        ), patch.object(
            readonly_qa.rachel_safety,
            "appfolio_readonly_thread_qa_allowed",
            return_value=(True, "allowed"),
        ), patch.object(
            readonly_qa, "get_appfolio_browser_manager", return_value=manager
        ), patch.object(
            readonly_qa.inbox,
            "inspect_one_appfolio_inbox_thread_read_only",
            return_value=metrics,
        ):
            self.assertEqual(readonly_qa.main(), 0)
        manager.operation.assert_called_once_with(
            "communication_inbox_single_thread_readonly_qa"
        )
        manager.close.assert_called_once_with()

    def test_readonly_qa_gate_requires_exact_configured_fingerprint(self):
        with patch.object(
            rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED",
            True,
        ), patch.object(
            rachel_safety, "RACHEL_ENABLED", False
        ), patch.object(
            rachel_safety, "RACHEL_TEST_MODE", True
        ), patch.object(
            rachel_safety, "RACHEL_APPFOLIO_QA_ENABLED", False
        ), patch.object(
            rachel_safety,
            "RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY",
            "e" * 64,
        ):
            self.assertEqual(
                rachel_safety.appfolio_readonly_thread_qa_allowed(
                    "e" * 64,
                    "Client Owner",
                    "owner-controlled-vendor-test",
                ),
                (True, "CONTROLLED READ-ONLY APPFOLIO THREAD QA"),
            )
            self.assertFalse(
                rachel_safety.appfolio_readonly_thread_qa_allowed(
                    "f" * 64,
                    "Client Owner",
                    "owner-controlled-vendor-test",
                )[0]
            )

    @staticmethod
    def _owned_row(index, *, owner="Rachel Asst", preview=None):
        row = list_row(
            thread_id=f"thread-{index}",
            contact_name=f"Safe Contact {index}",
            assigned_user=owner,
            preview=preview or f"Safe preview {index}",
        )
        row["selector_key"] = inbox_selector_key(row)
        row["event_key"] = inbox_event_key(row)
        return row

    def test_owned_thread_processor_opens_only_rachel_owned_rows(self):
        rows = [
            self._owned_row(1),
            self._owned_row(2, owner="Client Owner"),
            self._owned_row(3, owner=""),
        ]
        with tempfile.TemporaryDirectory() as root, patch.object(
            inbox,
            "ensure_appfolio_inbox_full_monitoring_scope",
            return_value={"verified_full_scope": True},
        ), patch.object(
            inbox, "_read_lightweight_inbox_rows", return_value=rows
        ), patch.object(
            inbox,
            "inspect_one_appfolio_inbox_thread_read_only",
            return_value={"appfolio_writes_attempted": 0},
        ) as inspect_thread:
            metrics = {}
            processed = inbox.process_rachel_owned_appfolio_threads(
                MagicMock(),
                db_path=os.path.join(root, "rachel.db"),
                metrics=metrics,
            )
        self.assertEqual(processed, 1)
        inspect_thread.assert_called_once()
        self.assertEqual(
            inspect_thread.call_args.kwargs["target_selector_key"],
            rows[0]["selector_key"],
        )
        self.assertIsNone(inspect_thread.call_args.kwargs["authorization"])
        self.assertEqual(metrics["human_or_unknown_thread_opens"], 0)
        self.assertEqual(metrics["appfolio_writes_attempted"], 0)
        self.assertEqual(metrics["outbound_messages_attempted"], 0)

    def test_owned_thread_processor_is_incremental_across_restart(self):
        row = self._owned_row(1)
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with patch.object(
                inbox,
                "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox, "_read_lightweight_inbox_rows", return_value=[row]
            ), patch.object(
                inbox,
                "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"appfolio_writes_attempted": 0},
            ) as inspect_thread:
                first_metrics = {}
                second_metrics = {}
                self.assertEqual(
                    inbox.process_rachel_owned_appfolio_threads(
                        MagicMock(), db_path=db_path, metrics=first_metrics
                    ),
                    1,
                )
                self.assertEqual(
                    inbox.process_rachel_owned_appfolio_threads(
                        MagicMock(), db_path=db_path, metrics=second_metrics
                    ),
                    0,
                )
            self.assertEqual(inspect_thread.call_count, 1)
            self.assertEqual(first_metrics["targeted_thread_opens"], 1)
            self.assertEqual(second_metrics["targeted_thread_opens"], 0)
            self.assertEqual(
                second_metrics["rachel_owned_unchanged_skipped"], 1
            )

    def test_owned_thread_processor_targets_changed_preview_once(self):
        original = self._owned_row(1, preview="Original inbound")
        changed = self._owned_row(1, preview="New inbound event")
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with patch.object(
                inbox,
                "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox,
                "_read_lightweight_inbox_rows",
                side_effect=[[original], [changed], [changed]],
            ), patch.object(
                inbox,
                "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"appfolio_writes_attempted": 0},
            ) as inspect_thread:
                for expected in (1, 1, 0):
                    self.assertEqual(
                        inbox.process_rachel_owned_appfolio_threads(
                            MagicMock(), db_path=db_path
                        ),
                        expected,
                    )
            self.assertEqual(inspect_thread.call_count, 2)
            self.assertNotEqual(
                original["selector_key"], changed["selector_key"]
            )

    def test_owned_thread_timestamp_reformat_does_not_reopen_read_event(self):
        original = self._owned_row(1)
        reformatted = dict(original, timestamp="Yesterday", unread=False)
        reformatted["event_key"] = inbox_event_key(reformatted)
        self.assertEqual(
            original["selector_key"], inbox_selector_key(reformatted)
        )
        self.assertNotEqual(original["event_key"], reformatted["event_key"])
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with patch.object(
                inbox,
                "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox,
                "_read_lightweight_inbox_rows",
                side_effect=[[original], [reformatted]],
            ), patch.object(
                inbox,
                "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"unread_after": False},
            ) as inspect_thread:
                self.assertEqual(
                    inbox.process_rachel_owned_appfolio_threads(
                        MagicMock(), db_path=db_path
                    ),
                    1,
                )
                self.assertEqual(
                    inbox.process_rachel_owned_appfolio_threads(
                        MagicMock(), db_path=db_path
                    ),
                    0,
                )
            self.assertEqual(inspect_thread.call_count, 1)

    def test_owned_thread_same_preview_new_unread_event_is_not_missed(self):
        original = self._owned_row(1)
        new_event = dict(
            original, timestamp="August 14 8:00 AM", unread=True
        )
        new_event["event_key"] = inbox_event_key(new_event)
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with patch.object(
                inbox,
                "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox,
                "_read_lightweight_inbox_rows",
                side_effect=[[original], [new_event]],
            ), patch.object(
                inbox,
                "inspect_one_appfolio_inbox_thread_read_only",
                return_value={"unread_after": False},
            ) as inspect_thread:
                first_metrics = {}
                second_metrics = {}
                inbox.process_rachel_owned_appfolio_threads(
                    MagicMock(), db_path=db_path, metrics=first_metrics
                )
                inbox.process_rachel_owned_appfolio_threads(
                    MagicMock(), db_path=db_path, metrics=second_metrics
                )
            self.assertEqual(inspect_thread.call_count, 2)
            self.assertEqual(
                second_metrics["target_reasons"],
                {"conversation_state_changed": 1},
            )

    def test_owned_thread_processor_has_no_arbitrary_scan_cap(self):
        rows = [self._owned_row(index) for index in range(9)]
        with tempfile.TemporaryDirectory() as root, patch.object(
            inbox,
            "ensure_appfolio_inbox_full_monitoring_scope",
            return_value={"verified_full_scope": True},
        ), patch.object(
            inbox, "_read_lightweight_inbox_rows", return_value=rows
        ), patch.object(
            inbox,
            "inspect_one_appfolio_inbox_thread_read_only",
            return_value={"appfolio_writes_attempted": 0},
        ) as inspect_thread:
            processed = inbox.process_rachel_owned_appfolio_threads(
                MagicMock(), db_path=os.path.join(root, "rachel.db")
            )
        self.assertEqual(processed, len(rows))
        self.assertEqual(inspect_thread.call_count, len(rows))

    def test_owned_thread_failure_is_durable_and_fails_the_runtime_poll(self):
        row = self._owned_row(1)
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            with patch.object(
                inbox,
                "ensure_appfolio_inbox_full_monitoring_scope",
                return_value={"verified_full_scope": True},
            ), patch.object(
                inbox, "_read_lightweight_inbox_rows", return_value=[row]
            ), patch.object(
                inbox,
                "inspect_one_appfolio_inbox_thread_read_only",
                side_effect=RuntimeError("raw browser detail"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "thread_read_failures=1"
                ):
                    inbox.process_rachel_owned_appfolio_threads(
                        MagicMock(), db_path=db_path
                    )
            conn = sqlite3.connect(db_path)
            status, error = conn.execute(
                "SELECT processing_status, last_error "
                "FROM appfolio_thread_selector_state"
            ).fetchone()
            conn.close()
            self.assertEqual(status, "failed")
            self.assertEqual(error, "RuntimeError")
            self.assertNotIn("raw browser detail", error)


if __name__ == "__main__":
    unittest.main()
