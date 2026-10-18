import os
import unittest
from unittest.mock import MagicMock, patch

for key in (
    "ANTHROPIC_API_KEY",
    "APPFOLIO_EMAIL",
    "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER",
    "RACHEL_GMAIL_PASSWORD",
    "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_work_order_scanner as scanner
import rachel_inbox_monitor as inbox_monitor


def record(wo_num, status="NEW", extra="Resident"):
    row_lines = [wo_num, status, extra]
    fields = scanner.extract_list_fields(
        wo_num, f"/maintenance/work_orders/{wo_num}", row_lines
    )
    return {
        "wo_num": wo_num,
        "status": status,
        "row_lines": row_lines,
        "wo_url": f"/maintenance/work_orders/{wo_num}",
        "list_fields": fields,
    }


class WorkOrderReconciliationTests(unittest.TestCase):
    def page(self):
        page = MagicMock()
        page.url = (
            "https://example.appfolio.com/maintenance/"
            "service_requests/work_orders"
        )
        return page

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "save_list_observation", return_value={})
    @patch.object(scanner, "inspect_work_order_detail")
    @patch.object(scanner, "load_scanner_states")
    @patch.object(scanner, "scan_all_pages")
    def test_restart_uses_stored_baseline_and_opens_no_details(
        self, scan_all, load_states, inspect_detail, save_list, _sleep
    ):
        current = record("1638-1")
        scan_all.return_value = [current]
        load_states.return_value = {
            "1638-1": {
                "wo_num": "1638-1",
                "list_fingerprint": scanner.list_record_fingerprint(current),
            }
        }
        counts = scanner.reconcile_logged_in_page(self.page())
        inspect_detail.assert_not_called()
        self.assertEqual(counts["unchanged_rows"], 1)
        self.assertFalse(counts["baseline_created"])
        self.assertEqual(counts["list_pages_scanned"], 0)
        self.assertEqual(counts["work_orders_listed"], 1)
        self.assertEqual(counts["targeted_detail_opens"], 0)
        self.assertEqual(counts["target_reasons"], {})
        self.assertEqual(counts["unchanged_work_orders_skipped"], 1)
        self.assertEqual(counts["detail_failures"], 0)
        self.assertEqual(
            counts["appfolio_navigations_attributable_to_reconciliation"],
            1,
        )
        self.assertEqual(counts["workflow_actions_attempted"], 0)
        self.assertGreaterEqual(counts["duration"], 0)

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "save_list_observation", return_value={})
    @patch.object(scanner, "inspect_work_order_detail")
    @patch.object(scanner, "load_scanner_states")
    @patch.object(scanner, "scan_all_pages")
    def test_navigation_metric_uses_shared_browser_counter_delta(
        self, scan_all, load_states, inspect_detail, _save_list, _sleep
    ):
        current = record("1638-1")
        scan_all.return_value = [current]
        load_states.return_value = {
            "1638-1": {
                "wo_num": "1638-1",
                "list_fingerprint": scanner.list_record_fingerprint(current),
            }
        }
        snapshots = iter((
            {"navigations": 100},
            {"navigations": 104},
        ))

        counts = scanner.reconcile_logged_in_page(
            self.page(),
            navigation_metrics_provider=lambda: next(snapshots),
        )

        inspect_detail.assert_not_called()
        self.assertEqual(
            counts["appfolio_navigations_attributable_to_reconciliation"],
            4,
        )

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "request_manager_review", return_value={})
    @patch.object(scanner, "save_scanner_observation", return_value={})
    @patch.object(scanner, "save_list_observation", return_value={})
    @patch.object(scanner, "classify_candidate", return_value="not_actionable_for_initial_triage")
    @patch.object(scanner, "inspect_work_order_detail")
    @patch.object(scanner, "load_scanner_states")
    @patch.object(scanner, "scan_all_pages")
    def test_new_and_changed_rows_trigger_only_targeted_details(
        self, scan_all, load_states, inspect_detail, _classify,
        _save_list, _save_detail, _request, _sleep
    ):
        unchanged = record("1000-1", "WAITING")
        changed = record("1001-1", "ASSIGNED")
        new = record("1002-1", "NEW")
        scan_all.return_value = [unchanged, changed, new]
        load_states.return_value = {
            "1000-1": {"list_fingerprint": scanner.list_record_fingerprint(unchanged)},
            "1001-1": {"list_fingerprint": "old-fingerprint"},
        }
        inspect_detail.side_effect = lambda _page, item: {
            **item, "live_status": item["status"], "actions_log": ""
        }
        counts = scanner.reconcile_logged_in_page(self.page())
        self.assertEqual(inspect_detail.call_count, 2)
        opened = {call.args[1]["wo_num"] for call in inspect_detail.call_args_list}
        self.assertEqual(opened, {"1001-1", "1002-1"})
        self.assertEqual(counts["targeted_detail_opens"], 2)
        self.assertEqual(
            counts["target_reasons"],
            {"list_fields_changed": 1, "unknown_work_order": 1},
        )
        self.assertEqual(
            counts["appfolio_navigations_attributable_to_reconciliation"],
            3,
        )

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "request_manager_review", return_value={})
    @patch.object(scanner, "save_scanner_observation", return_value={})
    @patch.object(scanner, "save_list_observation", return_value={})
    @patch.object(scanner, "classify_candidate", return_value="not_actionable_for_initial_triage")
    @patch.object(scanner, "inspect_work_order_detail")
    @patch.object(scanner, "load_scanner_states")
    @patch.object(scanner, "scan_all_pages")
    def test_missing_active_row_gets_one_targeted_integrity_check(
        self, scan_all, load_states, inspect_detail, _classify,
        save_list, _save_detail, _request, _sleep
    ):
        scan_all.return_value = []
        load_states.return_value = {
            "1003-1": {
                "wo_num": "1003-1",
                "wo_url": "/maintenance/work_orders/1003-1",
                "appfolio_status": "ASSIGNED",
                "list_fingerprint": "stored-list-fingerprint",
                "list_fields_json": "{}",
            },
            "0999-1": {
                "wo_num": "0999-1",
                "wo_url": "/maintenance/work_orders/0999-1",
                "appfolio_status": "COMPLETED",
                "list_fingerprint": "completed-fingerprint",
                "list_fields_json": "{}",
            },
        }
        inspect_detail.return_value = {
            "wo_num": "1003-1",
            "wo_url": "/maintenance/work_orders/1003-1",
            "live_status": "COMPLETED",
            "actions_log": "",
        }
        counts = scanner.reconcile_logged_in_page(self.page())
        inspect_detail.assert_called_once()
        self.assertEqual(
            inspect_detail.call_args.args[1]["scan_reason"],
            "missing_from_open_list",
        )
        save_list.assert_not_called()
        self.assertEqual(counts["missing_rows"], 1)
        self.assertEqual(counts["targeted_detail_opens"], 1)

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "request_manager_review")
    @patch.object(scanner, "send_sms_via_gmail")
    @patch.object(scanner, "save_scanner_observation", return_value={})
    @patch.object(scanner, "save_list_observation", return_value={})
    @patch.object(scanner, "inspect_work_order_detail")
    @patch.object(scanner, "load_scanner_states", return_value={})
    @patch.object(scanner, "scan_all_pages")
    def test_fresh_baseline_of_historical_rows_is_read_only(
        self,
        scan_all,
        _load_states,
        inspect_detail,
        _save_list,
        _save_detail,
        send_sms,
        request_review,
        _sleep,
    ):
        historical = [
            record("0900-1", "WAITING", "Fire extinguisher inspection"),
            record("0901-1", "ASSIGNED", "Resident"),
            record("0902-1", "SCHEDULED", "Resident"),
        ]
        scan_all.return_value = historical
        inspect_detail.side_effect = lambda _page, item: {
            **item,
            "live_status": item["status"],
            "job_desc": item["row_lines"][-1],
            "actions_log": "Historical assignment activity",
        }

        counts = scanner.reconcile_logged_in_page(self.page())

        self.assertTrue(counts["baseline_created"])
        self.assertEqual(counts["targeted_detail_opens"], 3)
        self.assertEqual(counts["actions_attempted"], 0)
        self.assertEqual(counts["workflow_actions_attempted"], 0)
        self.assertEqual(
            counts["target_reasons"], {"unknown_work_order": 3}
        )
        send_sms.assert_not_called()
        request_review.assert_not_called()

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "request_manager_review")
    @patch.object(scanner, "send_sms_via_gmail")
    @patch.object(scanner, "save_scanner_observation", return_value={})
    @patch.object(scanner, "save_list_observation", return_value={})
    @patch.object(scanner, "inspect_work_order_detail")
    @patch.object(scanner, "load_scanner_states")
    @patch.object(scanner, "scan_all_pages")
    def test_changed_assignment_or_status_never_starts_workflow(
        self,
        scan_all,
        load_states,
        inspect_detail,
        _save_list,
        save_detail,
        send_sms,
        request_review,
        _sleep,
    ):
        changed = record("1004-1", "ASSIGNED", "Fire extinguisher assigned")
        scan_all.return_value = [changed]
        load_states.return_value = {
            "1004-1": {
                "list_fingerprint": "previous-fingerprint",
                "classification": "manager_hold",
            }
        }
        inspect_detail.return_value = {
            **changed,
            "live_status": "ASSIGNED",
            "job_desc": "Fire extinguisher inspection",
            "actions_log": "Assignment changed",
        }

        counts = scanner.reconcile_logged_in_page(self.page())

        self.assertEqual(inspect_detail.call_count, 1)
        self.assertEqual(counts["targeted_detail_opens"], 1)
        self.assertEqual(
            counts["target_reasons"], {"list_fields_changed": 1}
        )
        self.assertEqual(
            counts["appfolio_navigations_attributable_to_reconciliation"],
            2,
        )
        self.assertEqual(counts["actions_attempted"], 0)
        self.assertEqual(counts["workflow_actions_attempted"], 0)
        saved = save_detail.call_args.args[0]
        self.assertEqual(saved["classification"], "manager_hold")
        self.assertEqual(saved["triage_origin"], "reconciliation")
        send_sms.assert_not_called()
        request_review.assert_not_called()

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "request_manager_review")
    @patch.object(scanner, "send_sms_via_gmail")
    @patch.object(scanner, "save_scanner_observation", return_value={})
    @patch.object(scanner, "save_list_observation", return_value={})
    @patch.object(scanner, "inspect_work_order_detail")
    @patch.object(scanner, "load_scanner_states")
    @patch.object(scanner, "scan_all_pages")
    def test_all_existing_status_list_changes_only_refresh_details(
        self,
        scan_all,
        load_states,
        inspect_detail,
        _save_list,
        _save_detail,
        send_sms,
        request_review,
        _sleep,
    ):
        changed = [
            record("1100-1", "WAITING"),
            record("1101-1", "ASSIGNED"),
            record("1102-1", "SCHEDULED"),
            record("1103-1", "WORK DONE"),
            record("1104-1", "CANCELED"),
            record("1105-1", "UNKNOWN"),
        ]
        scan_all.return_value = changed
        load_states.return_value = {
            item["wo_num"]: {"list_fingerprint": "old-fingerprint"}
            for item in changed
        }
        inspect_detail.side_effect = lambda _page, item: {
            **item,
            "live_status": item["status"],
            "priority": "EMERGENCY" if item["status"] == "WAITING" else "",
            "actions_log": "Existing history",
        }

        counts = scanner.reconcile_logged_in_page(self.page())

        self.assertEqual(counts["targeted_detail_opens"], 6)
        self.assertEqual(counts["actions_attempted"], 0)
        send_sms.assert_not_called()
        request_review.assert_not_called()

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "request_manager_review")
    @patch.object(scanner, "send_sms_via_gmail")
    @patch.object(scanner, "load_scanner_states", return_value={})
    @patch.object(scanner, "scan_all_pages")
    @patch.object(scanner, "inspect_work_order_detail")
    def test_failed_reconciliation_navigation_sends_no_false_notification(
        self,
        inspect_detail,
        scan_all,
        _load_states,
        send_sms,
        request_review,
        _sleep,
    ):
        # A detail-navigation failure propagates to the scheduler's retry
        # backoff; it must never be converted into a "New WO" workflow.
        scan_all.return_value = [record("1005-1", "NEW", "Resident")]
        inspect_detail.side_effect = TimeoutError("navigation failed")
        with self.assertRaises(TimeoutError):
            scanner.reconcile_logged_in_page(self.page())
        send_sms.assert_not_called()
        request_review.assert_not_called()

    @patch.object(scanner.time, "sleep", return_value=None)
    @patch.object(scanner, "load_scanner_states", return_value={})
    @patch.object(scanner, "scan_all_pages")
    @patch.object(scanner, "inspect_work_order_detail")
    @patch("builtins.print")
    def test_failed_detail_emits_aggregate_failure_metrics(
        self, print_mock, inspect_detail, scan_all, _load_states, _sleep
    ):
        scan_all.return_value = [record("1006-1", "NEW", "Resident")]
        inspect_detail.side_effect = TimeoutError("navigation failed")

        with self.assertRaises(TimeoutError):
            scanner.reconcile_logged_in_page(self.page())

        metrics_lines = [
            call.args[0]
            for call in print_mock.call_args_list
            if call.args and str(call.args[0]).startswith(
                "[ReconciliationMetrics]"
            )
        ]
        self.assertEqual(len(metrics_lines), 1)
        self.assertIn('"detail_failures": 1', metrics_lines[0])
        self.assertIn('"targeted_detail_opens": 1', metrics_lines[0])
        self.assertIn('"workflow_actions_attempted": 0', metrics_lines[0])

    def test_scan_all_pages_reports_pages_scanned(self):
        page = MagicMock()
        page.locator.return_value.all_inner_texts.return_value = []
        metrics = {}

        with patch.object(scanner, "scan_current_page", return_value=[]):
            scanner.scan_all_pages(page, metrics=metrics)

        self.assertEqual(metrics["list_pages_scanned"], 1)

    def test_failed_reconciliation_honors_persistent_backoff(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            original_path = inbox_monitor.DB_PATH
            inbox_monitor.DB_PATH = os.path.join(root, "runtime.db")
            try:
                inbox_monitor._mark_work_order_reconciliation_failed()
                self.assertFalse(inbox_monitor._work_order_reconciliation_due())
            finally:
                inbox_monitor.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
