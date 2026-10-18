"""Controlled one-thread, read-only AppFolio Communication Inbox QA."""

import json
from urllib.parse import urlsplit

import rachel_appfolio_inbox as inbox
import rachel_safety
from rachel_appfolio_browser import get_appfolio_browser_manager


_ALLOWED_FRAME_EVALUATIONS = {
    inbox._INBOX_FRAME_DIAGNOSTICS_SCRIPT,
    inbox._INBOX_LIGHTWEIGHT_ROWS_SCRIPT,
    inbox._INBOX_SCROLL_PROGRESS_SCRIPT,
    inbox._INBOX_SEMANTIC_RETURN_VERIFICATION_SCRIPT,
    inbox._INBOX_EXACT_ROW_MATCH_SCRIPT,
    inbox._INBOX_EXACT_ROW_CLICK_SCRIPT,
    inbox._INBOX_THREAD_READY_SCRIPT,
    inbox._INBOX_THREAD_READ_SCRIPT,
    inbox._INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT,
    inbox._INBOX_BACK_TO_INBOX_COUNT_SCRIPT,
    inbox._INBOX_BACK_TO_INBOX_SCRIPT,
}
_ALLOWED_PAGE_EVALUATIONS = {
    inbox._INBOX_FILTER_SCOPE_RESET_SCRIPT,
}


class _ReadOnlyGuardState:
    def __init__(self):
        self.blocked_actions = 0
        self.inbox_navigations = 0


class _ReadOnlyThreadFrame:
    def __init__(self, frame, state):
        self._frame = frame
        self._state = state

    def evaluate(self, script, *args):
        if script not in _ALLOWED_FRAME_EVALUATIONS:
            self._state.blocked_actions += 1
            raise RuntimeError("read-only thread QA blocked frame evaluation")
        return self._frame.evaluate(script, *args)

    def __getattr__(self, name):
        if name in {
            "click", "dblclick", "dispatch_event", "fill", "press", "type",
            "check", "uncheck", "select_option", "set_input_files",
        }:
            self._state.blocked_actions += 1
            raise RuntimeError(f"read-only thread QA blocked frame action: {name}")
        return getattr(self._frame, name)


class _ReadOnlyThreadPage:
    def __init__(self, page, state):
        self._page = page
        self._state = state

    @property
    def frames(self):
        return [
            _ReadOnlyThreadFrame(frame, self._state)
            for frame in list(self._page.frames)
        ]

    @property
    def url(self):
        return self._page.url

    def goto(self, url, **kwargs):
        requested = urlsplit(str(url or ""))
        expected = urlsplit(inbox.INBOX_URL)
        if (
            requested.scheme != expected.scheme
            or requested.netloc != expected.netloc
            or requested.path.rstrip("/") != expected.path.rstrip("/")
            or requested.query
            or requested.fragment
        ):
            self._state.blocked_actions += 1
            raise RuntimeError("read-only thread QA blocked non-inbox navigation")
        self._state.inbox_navigations += 1
        return self._page.goto(url, **kwargs)

    def evaluate(self, script, *args):
        if script not in _ALLOWED_PAGE_EVALUATIONS:
            self._state.blocked_actions += 1
            raise RuntimeError("read-only thread QA blocked page evaluation")
        return self._page.evaluate(script, *args)

    def wait_for_timeout(self, milliseconds):
        return self._page.wait_for_timeout(milliseconds)

    def __getattr__(self, name):
        if name in {
            "click", "dblclick", "dispatch_event", "fill", "press", "type",
            "check", "uncheck", "select_option", "set_input_files",
            "locator", "get_by_role", "get_by_text", "query_selector",
            "keyboard", "mouse",
        }:
            self._state.blocked_actions += 1
            raise RuntimeError(f"read-only thread QA blocked page action: {name}")
        return getattr(self._page, name)


def main() -> int:
    target_selector_key = (
        rachel_safety.RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY
    )
    expected_event_key = (
        rachel_safety.RACHEL_APPFOLIO_READONLY_THREAD_QA_EXPECTED_EVENT_KEY
    )
    delegated_by = (
        rachel_safety.RACHEL_APPFOLIO_READONLY_THREAD_QA_DELEGATED_BY
    )
    authorization_id = (
        rachel_safety.RACHEL_APPFOLIO_READONLY_THREAD_QA_AUTHORIZATION_ID
    )
    allowed, reason = rachel_safety.appfolio_readonly_thread_qa_allowed(
        target_selector_key,
        delegated_by,
        authorization_id,
    )
    if not allowed:
        raise RuntimeError(reason)

    manager = get_appfolio_browser_manager()
    state = _ReadOnlyGuardState()
    try:
        with manager.operation(
            "communication_inbox_single_thread_readonly_qa"
        ) as page:
            original_page_identity = id(page)
            original_context_identity = id(getattr(page, "context", None))
            guarded_page = _ReadOnlyThreadPage(page, state)
            metrics = inbox.inspect_one_appfolio_inbox_thread_read_only(
                guarded_page,
                target_selector_key=target_selector_key,
                expected_event_key=expected_event_key,
                authorization={
                    "kind": "explicit_delegation",
                    "selector_key": target_selector_key,
                    "delegated_by": delegated_by,
                    "authorization_id": authorization_id,
                },
                db_path=inbox.DB_PATH,
            )
            same_page = id(page) == original_page_identity
            same_context = (
                id(getattr(page, "context", None))
                == original_context_identity
            )
        browser_metrics = manager.metrics
        stage_trace = list(metrics.get("stage_trace") or [])
        _context_reused = bool(same_page and same_context)
        stage_trace.append({
            "stage": "browser_context_reused",
            "status": "passed" if _context_reused else "failed",
            "reason": (
                "same_page_and_context_reused"
                if _context_reused else "page_or_context_changed"
            ),
        })
        _zero_write_accounting = bool(
            state.blocked_actions == 0
            and int(metrics.get("appfolio_writes_attempted", 0)) == 0
            and int(metrics.get("outbound_messages_attempted", 0)) == 0
            and int(metrics.get("composer_inputs_attempted", 0)) == 0
            and int(metrics.get("read_state_toggles_attempted", 0)) == 0
            and int(metrics.get("assignment_changes_attempted", 0)) == 0
            and int(metrics.get("overflow_actions_attempted", 0)) == 0
            and int(metrics.get("scheduling_actions_attempted", 0)) == 0
        )
        stage_trace.append({
            "stage": "zero_write_accounting",
            "status": "passed" if _zero_write_accounting else "failed",
            "reason": (
                "all_prohibited_action_counters_zero"
                if _zero_write_accounting else "prohibited_action_counter_nonzero"
            ),
        })
        sanitized_report = {
            "schema_version": 1,
            "sanitized": True,
            "rows_seen": int(metrics.get("rows_seen", 0)),
            "unique_rows_seen": int(metrics.get("unique_rows_seen", 0)),
            "target_resolution_iterations": int(
                metrics.get("target_resolution_iterations", 0)
            ),
            "target_resolution_end_condition": metrics.get(
                "target_resolution_end_condition", ""
            ),
            "target_match_count": int(metrics.get("target_match_count", 0)),
            "selected_row_count": int(
                metrics.get("selected_row_count", 0)
            ),
            "conversation_opens": int(metrics.get("conversation_opens", 0)),
            "identity_fields_compared": int(
                metrics.get("identity_fields_compared", 0)
            ),
            "identity_fields_matched": int(
                metrics.get("identity_fields_matched", 0)
            ),
            "messages_parsed": int(metrics.get("messages_parsed", 0)),
            "messages_stored": int(metrics.get("messages_stored", 0)),
            "messages_fingerprinted": int(
                metrics.get("messages_fingerprinted", 0)
            ),
            "unique_message_fingerprints": int(
                metrics.get("unique_message_fingerprints", 0)
            ),
            "messages_previously_stored": int(
                metrics.get("messages_previously_stored", 0)
            ),
            "messages_newly_stored": int(
                metrics.get("messages_newly_stored", 0)
            ),
            "durable_message_rows_after": int(
                metrics.get("durable_message_rows_after", 0)
            ),
            "message_deduplication_verified": bool(
                metrics.get("message_deduplication_verified")
            ),
            "explicit_wo_relationships": int(
                metrics.get("explicit_wo_relationships", 0)
            ),
            "unread_before": bool(metrics.get("unread_before")),
            "unread_after": bool(metrics.get("unread_after")),
            "unread_to_read_transition": bool(
                metrics.get("unread_to_read_transition")
            ),
            "opening_treated_as_global_read_mutation": bool(
                metrics.get("opening_treated_as_global_read_mutation")
            ),
            "expected_event_fingerprint_matched": bool(
                metrics.get("expected_event_fingerprint_matched")
            ),
            "volatile_timestamp_present": bool(
                metrics.get("volatile_timestamp_present")
            ),
            "open_authorization_reason": metrics.get(
                "open_authorization_reason", ""
            ),
            "history_scrollable": bool(metrics.get("history_scrollable")),
            "history_at_scroll_start": bool(
                metrics.get("history_at_scroll_start")
            ),
            "history_at_scroll_end": bool(
                metrics.get("history_at_scroll_end")
            ),
            "virtualization_hints_present": bool(
                metrics.get("virtualization_hints_present")
            ),
            "lazy_load_hints_present": bool(
                metrics.get("lazy_load_hints_present")
            ),
            "rendered_message_item_count": int(
                metrics.get("rendered_message_item_count", 0)
            ),
            "invalid_message_structure_count": int(
                metrics.get("invalid_message_structure_count", 0)
            ),
            "readiness_attempts": int(
                metrics.get("readiness_attempts", 0)
            ),
            "filter_scope_reverified": bool(
                metrics.get("filter_scope_reverified")
            ),
            "returned_to_inbox": bool(metrics.get("returned_to_inbox")),
            "same_page_reused": same_page,
            "same_context_reused": same_context,
            "browser_context_starts": int(
                browser_metrics.get("context_starts", 0)
            ),
            "inbox_navigations": state.inbox_navigations,
            "guard_blocked_actions": state.blocked_actions,
            "stage_trace": stage_trace,
            "appfolio_writes_attempted": int(
                metrics.get("appfolio_writes_attempted", 0)
            ),
            "outbound_messages_attempted": int(
                metrics.get("outbound_messages_attempted", 0)
            ),
            "composer_inputs_attempted": int(
                metrics.get("composer_inputs_attempted", 0)
            ),
            "read_state_toggles_attempted": int(
                metrics.get("read_state_toggles_attempted", 0)
            ),
            "assignment_changes_attempted": int(
                metrics.get("assignment_changes_attempted", 0)
            ),
            "overflow_actions_attempted": int(
                metrics.get("overflow_actions_attempted", 0)
            ),
            "scheduling_actions_attempted": int(
                metrics.get("scheduling_actions_attempted", 0)
            ),
            "duration_seconds": float(metrics.get("duration_seconds", 0)),
        }
        sanitized_report["passed"] = all([
            sanitized_report["selected_row_count"] == 1,
            sanitized_report["conversation_opens"] == 1,
            sanitized_report["opening_treated_as_global_read_mutation"],
            sanitized_report["open_authorization_reason"] in {
                "thread_owned_by_rachel",
                "thread_started_by_rachel",
                "explicitly_delegated_to_rachel",
                "workflow_authorized_takeover",
            },
            sanitized_report["identity_fields_compared"] >= 2,
            sanitized_report["identity_fields_matched"]
            == sanitized_report["identity_fields_compared"],
            sanitized_report["messages_parsed"] >= 1,
            sanitized_report["rendered_message_item_count"]
            == sanitized_report["messages_parsed"],
            sanitized_report["messages_fingerprinted"]
            == sanitized_report["messages_parsed"],
            sanitized_report["messages_stored"]
            == sanitized_report["messages_parsed"],
            sanitized_report["unique_message_fingerprints"]
            == sanitized_report["messages_parsed"],
            sanitized_report["durable_message_rows_after"]
            == sanitized_report["messages_parsed"],
            sanitized_report["message_deduplication_verified"],
            sanitized_report["invalid_message_structure_count"] == 0,
            sanitized_report["readiness_attempts"] >= 1,
            sanitized_report["filter_scope_reverified"],
            sanitized_report["returned_to_inbox"],
            sanitized_report["same_page_reused"],
            sanitized_report["same_context_reused"],
            sanitized_report["guard_blocked_actions"] == 0,
            sanitized_report["appfolio_writes_attempted"] == 0,
            sanitized_report["outbound_messages_attempted"] == 0,
            sanitized_report["composer_inputs_attempted"] == 0,
            sanitized_report["read_state_toggles_attempted"] == 0,
            sanitized_report["assignment_changes_attempted"] == 0,
            sanitized_report["overflow_actions_attempted"] == 0,
            sanitized_report["scheduling_actions_attempted"] == 0,
            all(
                any(
                    entry.get("stage") == required
                    and entry.get("status") == "passed"
                    for entry in sanitized_report["stage_trace"]
                )
                for required in (
                    "target_resolved", "authorization_verified",
                    "thread_clicked", "thread_ready", "identity_verified",
                    "history_verified", "messages_parsed",
                    "fingerprints_created", "durable_upsert_complete",
                    "semantic_postcheck_passed", "returned_to_inbox",
                    "browser_context_reused", "zero_write_accounting",
                )
            ),
        ])
        print(
            "[AF-InboxThreadReadOnlyQA] "
            + json.dumps(sanitized_report, sort_keys=True)
        )
        return 0 if sanitized_report["passed"] else 1
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
