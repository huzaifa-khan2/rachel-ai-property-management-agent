"""Two-poll production-path QA for the AppFolio Communication Inbox."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import rachel_db_init
from rachel_appfolio_browser import (
    get_appfolio_browser_manager,
    get_downloads_dir,
)
from rachel_appfolio_inbox import (
    INBOX_URL,
    _INBOX_FILTER_SCOPE_RESET_SCRIPT,
    _INBOX_FRAME_DIAGNOSTICS_SCRIPT,
    _INBOX_LIGHTWEIGHT_ROWS_SCRIPT,
    check_appfolio_inbox,
)


TRUE_VALUES = {"1", "true", "yes", "on"}
_FRAME_EVALUATIONS = {
    _INBOX_FRAME_DIAGNOSTICS_SCRIPT,
    _INBOX_LIGHTWEIGHT_ROWS_SCRIPT,
}
_PAGE_EVALUATIONS = {
    _INBOX_FILTER_SCOPE_RESET_SCRIPT,
}


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_VALUES


class _GuardState:
    def __init__(self) -> None:
        self.inbox_navigations = 0
        self.blocked_actions = 0


class _ReadOnlyInboxFrame:
    def __init__(self, frame, state: _GuardState) -> None:
        self._frame = frame
        self._state = state

    def evaluate(self, script, *args):
        if script not in _FRAME_EVALUATIONS:
            self._state.blocked_actions += 1
            raise RuntimeError(
                "Combined inbox QA blocked a non-list frame evaluation"
            )
        return self._frame.evaluate(script, *args)

    def __getattr__(self, name):
        if name in {
            "click", "dblclick", "dispatch_event", "fill", "press", "type"
        }:
            self._state.blocked_actions += 1
            raise RuntimeError(
                "Combined inbox QA blocked an interactive frame action"
            )
        return getattr(self._frame, name)


class _ReadOnlyInboxPage:
    def __init__(self, page, state: _GuardState) -> None:
        self._page = page
        self._state = state

    @property
    def frames(self):
        return [
            _ReadOnlyInboxFrame(frame, self._state)
            for frame in list(self._page.frames)
        ]

    def goto(self, url, **kwargs):
        requested = urlsplit(str(url or ""))
        expected = urlsplit(INBOX_URL)
        if (
            requested.scheme != expected.scheme
            or requested.netloc != expected.netloc
            or requested.path.rstrip("/") != expected.path.rstrip("/")
            or requested.query
            or requested.fragment
        ):
            self._state.blocked_actions += 1
            raise RuntimeError(
                "Combined inbox QA blocked navigation outside the inbox list"
            )
        self._state.inbox_navigations += 1
        return self._page.goto(url, **kwargs)

    def evaluate(self, script, *args):
        if script not in _PAGE_EVALUATIONS:
            self._state.blocked_actions += 1
            raise RuntimeError(
                "Combined inbox QA blocked a non-filter page evaluation"
            )
        return self._page.evaluate(script, *args)

    def wait_for_timeout(self, milliseconds):
        return self._page.wait_for_timeout(milliseconds)

    def __getattr__(self, name):
        if name in {
            "click", "dblclick", "dispatch_event", "fill", "press", "type",
            "locator", "get_by_role", "get_by_text", "query_selector",
        }:
            self._state.blocked_actions += 1
            raise RuntimeError(
                "Combined inbox QA blocked an interactive page action"
            )
        return getattr(self._page, name)


def _database_counts(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "fingerprints": int(conn.execute(
                "SELECT COUNT(*) FROM appfolio_communication_state"
            ).fetchone()[0]),
            "communications": int(conn.execute(
                "SELECT COUNT(*) FROM communications"
            ).fetchone()[0]),
        }
    finally:
        conn.close()


def _validate_environment() -> None:
    if not _enabled("RACHEL_TEST_MODE"):
        raise RuntimeError("RACHEL_TEST_MODE must be enabled")
    if _enabled("RACHEL_ENABLED"):
        raise RuntimeError("RACHEL_ENABLED must be false for combined QA")
    if _enabled("RACHEL_APPFOLIO_QA_ENABLED"):
        raise RuntimeError("AppFolio write QA must be disabled")
    if _enabled("RACHEL_SMS_ENABLED"):
        raise RuntimeError("SMS must be disabled for combined QA")


def main() -> int:
    _validate_environment()
    manager = get_appfolio_browser_manager()
    state = _GuardState()
    original_db_path = rachel_db_init.DB_PATH
    try:
        with tempfile.TemporaryDirectory(
            prefix="rachel_communication_inbox_qa_"
        ) as temporary_root:
            qa_db_path = str(Path(temporary_root) / "inbox_qa.db")
            rachel_db_init.DB_PATH = qa_db_path
            rachel_db_init.initialize_database()

            poll_1 = {}
            poll_2 = {}
            with manager.operation("communication_inbox_combined_qa_poll_1") as page_1:
                context_1 = getattr(page_1, "context", None)
                page_identity_1 = id(page_1)
                context_identity_1 = id(context_1)
                guarded_page_1 = _ReadOnlyInboxPage(page_1, state)
                check_appfolio_inbox(
                    guarded_page_1, db_path=qa_db_path, metrics=poll_1
                )
            counts_after_poll_1 = _database_counts(qa_db_path)

            with manager.operation("communication_inbox_combined_qa_poll_2") as page_2:
                context_2 = getattr(page_2, "context", None)
                page_identity_2 = id(page_2)
                context_identity_2 = id(context_2)
                guarded_page_2 = _ReadOnlyInboxPage(page_2, state)
                check_appfolio_inbox(
                    guarded_page_2, db_path=qa_db_path, metrics=poll_2
                )
            counts_after_poll_2 = _database_counts(qa_db_path)

            browser_metrics = manager.metrics
            same_context_reused = (
                page_identity_1 == page_identity_2
                and context_identity_1 == context_identity_2
            )
            communication_growth_poll_2 = (
                counts_after_poll_2["communications"]
                - counts_after_poll_1["communications"]
            )
            duplicate_events_poll_2 = max(
                0,
                int(poll_2.get("stored_events", 0))
                - communication_growth_poll_2,
            ) + int(poll_2.get("duplicate_event_fingerprints", 0))
            conversation_opens = (
                int(poll_1.get("conversation_opens", 0))
                + int(poll_2.get("conversation_opens", 0))
            )
            conversation_clicks = (
                int(poll_1.get("conversation_clicks", 0))
                + int(poll_2.get("conversation_clicks", 0))
            )
            writes_messages_attempted = sum([
                int(poll_1.get("appfolio_writes_attempted", 0)),
                int(poll_2.get("appfolio_writes_attempted", 0)),
                int(poll_1.get("outbound_messages_attempted", 0)),
                int(poll_2.get("outbound_messages_attempted", 0)),
            ])

            success_checks = {
                "context_starts_one":
                    browser_metrics.get("context_starts") == 1,
                "successful_login_present":
                    browser_metrics.get("successful_logins", 0) >= 1,
                "same_context_reused": same_context_reused,
                "poll_1_full_scope":
                    poll_1.get("filter_status") == "full_scope_verified",
                "poll_2_full_scope":
                    poll_2.get("filter_status") == "full_scope_verified",
                "poll_1_detected_full_inbox":
                    int(poll_1.get("rows_seen", 0)) > 1,
                "poll_row_counts_match":
                    poll_1.get("rows_seen") == poll_2.get("rows_seen"),
                "poll_2_has_no_new_events":
                    int(poll_2.get("newly_observed", -1)) == 0,
                "poll_2_has_no_duplicates": duplicate_events_poll_2 == 0,
                "no_conversation_opens": conversation_opens == 0,
                "no_conversation_clicks": conversation_clicks == 0,
                "no_appfolio_writes_or_messages":
                    writes_messages_attempted == 0,
                "qa_guard_blocked_nothing": state.blocked_actions == 0,
            }
            report = {
                "schema_version": 1,
                "sanitized": True,
                "browser_context_starts":
                    browser_metrics.get("context_starts", 0),
                "successful_logins":
                    browser_metrics.get("successful_logins", 0),
                "same_context_reused": same_context_reused,
                "poll_1": {
                    "filter_status": poll_1.get("filter_status", "unknown"),
                    "rows_seen": int(poll_1.get("rows_seen", 0)),
                    "newly_observed": int(
                        poll_1.get("newly_observed", 0)
                    ),
                    "already_observed": int(
                        poll_1.get("already_observed", 0)
                    ),
                    "conversation_opens": int(
                        poll_1.get("conversation_opens", 0)
                    ),
                },
                "poll_2": {
                    "filter_status": poll_2.get("filter_status", "unknown"),
                    "rows_seen": int(poll_2.get("rows_seen", 0)),
                    "newly_observed": int(
                        poll_2.get("newly_observed", 0)
                    ),
                    "already_observed": int(
                        poll_2.get("already_observed", 0)
                    ),
                    "duplicate_events": duplicate_events_poll_2,
                    "conversation_opens": int(
                        poll_2.get("conversation_opens", 0)
                    ),
                },
                "conversation_clicks": conversation_clicks,
                "writes_messages_attempted": writes_messages_attempted,
                "inbox_list_navigations": state.inbox_navigations,
                "qa_guard_blocked_actions": state.blocked_actions,
                "temporary_qa_fingerprints_after_poll_1":
                    counts_after_poll_1["fingerprints"],
                "temporary_qa_fingerprints_after_poll_2":
                    counts_after_poll_2["fingerprints"],
                "success_checks": success_checks,
                "passed": all(success_checks.values()),
            }

            output_dir = Path(get_downloads_dir())
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output_path = output_dir / (
                "appfolio_communication_inbox_combined_qa_"
                f"{timestamp}.json"
            )
            output_path.write_text(
                json.dumps(report, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print("[AF-InboxCombinedQA] " + json.dumps(report, sort_keys=True))
            print(f"[AF-InboxCombinedQA] Report: {output_path}")
            return 0 if report["passed"] else 1
    finally:
        rachel_db_init.DB_PATH = original_db_path
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
