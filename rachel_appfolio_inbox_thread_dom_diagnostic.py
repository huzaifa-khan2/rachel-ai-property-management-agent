"""One-shot sanitized diagnostic for one explicitly delegated inbox thread."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import rachel_appfolio_inbox as inbox
import rachel_safety
from rachel_appfolio_browser import (
    get_appfolio_browser_manager,
    get_downloads_dir,
)
from rachel_appfolio_inbox_thread_readonly_qa import (
    _ReadOnlyGuardState,
    _ReadOnlyThreadPage,
)


def _authorization() -> tuple[str, str, str, str]:
    selector_key = (
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
        selector_key,
        delegated_by,
        authorization_id,
    )
    if not allowed:
        raise RuntimeError(reason)
    return selector_key, expected_event_key, delegated_by, authorization_id


def main() -> int:
    (
        target_selector_key,
        expected_event_key,
        delegated_by,
        authorization_id,
    ) = _authorization()
    manager = get_appfolio_browser_manager()
    state = _ReadOnlyGuardState()
    try:
        with manager.operation(
            "communication_inbox_delegated_thread_dom_diagnostic"
        ) as page:
            original_page_identity = id(page)
            original_context_identity = id(getattr(page, "context", None))
            guarded_page = _ReadOnlyThreadPage(page, state)
            guarded_page.goto(
                inbox.INBOX_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            guarded_page.wait_for_timeout(3000)
            inbox.ensure_appfolio_inbox_full_monitoring_scope(guarded_page)
            rows = inbox._read_lightweight_inbox_rows(guarded_page)
            selected = inbox._target_row_for_selector_key(
                rows, target_selector_key
            )
            authorization = inbox._authorize_inbox_thread_open(
                selected,
                authorization={
                    "kind": "explicit_delegation",
                    "selector_key": target_selector_key,
                    "delegated_by": delegated_by,
                    "authorization_id": authorization_id,
                },
            )
            expected_event_matched = bool(
                str(expected_event_key or "").strip().lower()
                == str(selected.get("event_key", "") or "").strip().lower()
            )
            click_evidence = inbox._click_exact_inbox_row(
                guarded_page, selected
            )
            guarded_page.wait_for_timeout(1200)
            report = None
            return_frame = None
            try:
                report, return_frame = (
                    inbox.diagnose_opened_appfolio_inbox_thread_structure(
                        guarded_page
                    )
                )
            finally:
                inbox._return_to_inbox(
                    guarded_page,
                    {"verified_frame": return_frame}
                    if return_frame is not None else {},
                )
            scope = inbox.ensure_appfolio_inbox_full_monitoring_scope(
                guarded_page
            )
            same_page = id(page) == original_page_identity
            same_context = (
                id(getattr(page, "context", None))
                == original_context_identity
            )

        if report is None:
            raise RuntimeError("opened-thread diagnostic produced no report")
        report["delegated_open_summary"] = {
            "rows_seen": len(rows),
            "selected_row_count": int(
                click_evidence.get("selected_row_count", 0) or 0
            ),
            "conversation_opens": 1,
            "open_authorization_reason": authorization.get("reason", ""),
            "stable_selector_authorized": True,
            "historical_event_fingerprint_matched": expected_event_matched,
            "volatile_event_fingerprint_required_for_authorization": False,
            "filter_scope_reverified": bool(
                scope.get("verified_full_scope")
            ),
            "same_page_reused": same_page,
            "same_context_reused": same_context,
        }
        report["safety"] = {
            "read_only": True,
            "exact_delegated_thread_opens": 1,
            "composer_inputs_attempted": 0,
            "outbound_messages_attempted": 0,
            "read_state_toggles_attempted": 0,
            "assignment_changes_attempted": 0,
            "overflow_actions_attempted": 0,
            "scheduling_actions_attempted": 0,
            "appfolio_writes_attempted": 0,
            "guard_blocked_actions": state.blocked_actions,
            "inbox_navigations": state.inbox_navigations,
        }
        report["browser_metrics"] = manager.metrics
        inbox._validate_sanitized_dom_diagnostic(report)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = Path(get_downloads_dir())
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / (
            f"appfolio_opened_thread_dom_sanitized_{timestamp}.json"
        )
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            "[AF-OpenedThreadDOM] Sanitized diagnostic complete "
            f"frames={report['frame_count']} "
            "legacy_verified_frames="
            f"{report['legacy_verified_frame_count']} "
            "back_controls="
            f"{report['back_to_inbox_control_count']}"
        )
        print(f"[AF-OpenedThreadDOM] Report: {output_path}")
        print(
            "[AF-OpenedThreadDOM] Safety: exact delegated open=1, "
            "writes=0, inputs=0, sends=0, toggles=0, assignments=0, "
            "overflow actions=0, scheduling=0"
        )
        return 0
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
