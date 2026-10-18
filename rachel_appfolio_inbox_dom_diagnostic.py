"""One-shot sanitized AppFolio Communication Inbox DOM diagnostic."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from rachel_appfolio_browser import (
    get_appfolio_browser_manager,
    get_downloads_dir,
)
from rachel_appfolio_inbox import (
    INBOX_URL,
    _read_lightweight_inbox_rows,
    diagnose_appfolio_inbox_row_structure,
    ensure_appfolio_inbox_full_monitoring_scope,
)


TRUE_VALUES = {"1", "true", "yes", "on"}


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_VALUES


def main() -> int:
    if not _enabled("RACHEL_TEST_MODE"):
        raise RuntimeError(
            "RACHEL_TEST_MODE must be enabled for the DOM diagnostic"
        )
    if _enabled("RACHEL_APPFOLIO_QA_ENABLED"):
        raise RuntimeError(
            "Controlled AppFolio write QA must be disabled for this diagnostic"
        )

    manager = get_appfolio_browser_manager()
    try:
        with manager.operation("read_only_inbox_dom_diagnostic") as page:
            page.goto(
                INBOX_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(5000)
            filter_scope = ensure_appfolio_inbox_full_monitoring_scope(page)
            report = diagnose_appfolio_inbox_row_structure(page)
            rows = _read_lightweight_inbox_rows(page)

        report["browser_metrics"] = manager.metrics
        report["filter_scope_summary"] = filter_scope
        report["extraction_summary"] = {
            "rows_detected": len(rows),
            "unique_thread_identities": len({
                row.get("thread_id", "") for row in rows
                if row.get("thread_id")
            }),
            "unique_event_identities": len({
                row.get("source_message_id", "") for row in rows
                if row.get("source_message_id")
            }),
            "unread_rows_detected": sum(
                1 for row in rows if row.get("unread")
            ),
            "raw_row_content_stored_in_report": False,
        }
        report["safety"] = {
            "read_only": True,
            "conversation_rows_clicked": 0,
            "threads_opened": 0,
            "communications_modified": 0,
            "list_filter_state_modified": int(
                filter_scope.get("filter_clear_actions", 0) or 0
            ),
        }
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = Path(get_downloads_dir())
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / (
            f"appfolio_inbox_dom_sanitized_{timestamp}.json"
        )
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        candidate_groups = sum(
            int(
                frame.get("structure", {}).get(
                    "candidate_group_count", 0
                ) or 0
            )
            for frame in report["frames"]
        )
        print(
            "[AF-InboxDOM] Sanitized diagnostic complete "
            f"frames={report['frame_count']} "
            f"candidate_groups={candidate_groups}"
        )
        print(f"[AF-InboxDOM] Report: {output_path}")
        print(
            "[AF-InboxDOM] Read-only row extraction "
            f"rows={report['extraction_summary']['rows_detected']} "
            "unique_threads="
            f"{report['extraction_summary']['unique_thread_identities']} "
            "unique_events="
            f"{report['extraction_summary']['unique_event_identities']} "
            "unread="
            f"{report['extraction_summary']['unread_rows_detected']}"
        )
        print(
            "[AF-InboxDOM] No conversation rows were clicked or opened."
        )
        return 0
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
