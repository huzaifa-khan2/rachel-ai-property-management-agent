import os
import json
import sqlite3
import tempfile
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

import rachel_wo_triage as triage
import rachel_db_init
import rachel_inbox_monitor as monitor


EXACT_URL = (
    "https://clientco.appfolio.com/maintenance/"
    "service_requests/17/work_orders/1700"
)


def details(**overrides):
    payload = {
        "triage_origin": "original_new_work_order_email",
        "source_type": "RESIDENT",
        "wo_num": "1700-1",
        "wo_url": EXACT_URL,
        "tenant": "Test Resident",
        "address": "Test Property",
        "job_desc": "Kitchen faucet drips",
    }
    payload.update(overrides)
    return payload


class WorkOrderRoutingSafetyTests(unittest.TestCase):
    def test_inactive_vendors_are_never_recommended(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            original_triage_db = triage.DB_PATH
            original_init_db = rachel_db_init.DB_PATH
            triage.DB_PATH = db_path
            rachel_db_init.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "INSERT INTO vendors (name, trade, active) "
                    "VALUES ('Inactive Plumbing', 'plumbing', 0)"
                )
                inactive_id = conn.execute(
                    "SELECT id FROM vendors WHERE name = 'Inactive Plumbing'"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO property_vendors "
                    "(property_address, vendor_id, trade, source) "
                    "VALUES ('100 Oak Street', ?, 'plumbing', 'test')",
                    (inactive_id,),
                )
                conn.execute(
                    "INSERT INTO vendors (name, trade, active) "
                    "VALUES ('Active Plumbing', 'plumbing', 1)"
                )
                conn.commit()
                conn.close()

                suggestions = triage.get_suggested_vendors(
                    "100 Oak Street", "plumbing"
                )
                names = {row["name"] for row in suggestions}

                self.assertNotIn("Inactive Plumbing", names)
                self.assertIn("Active Plumbing", names)
            finally:
                triage.DB_PATH = original_triage_db
                rachel_db_init.DB_PATH = original_init_db

    def test_vendor_removal_requires_an_exact_name(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            original_monitor_db = monitor.DB_PATH
            original_init_db = rachel_db_init.DB_PATH
            monitor.DB_PATH = db_path
            rachel_db_init.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                conn = sqlite3.connect(db_path)
                conn.executemany(
                    "INSERT INTO vendors (name, trade, active) VALUES (?, ?, 1)",
                    [
                        ("Alpha Plumbing", "plumbing"),
                        ("Alpha Plumbing North", "plumbing"),
                    ],
                )
                conn.commit()
                conn.close()

                with patch.object(
                    monitor,
                    "resolve_internal_user",
                    return_value={"user_id": "authorized-manager"},
                ), patch.object(
                    monitor, "internal_user_has_access", return_value=True
                ), patch.object(monitor, "send_email", return_value=True):
                    result = monitor.handle_vendor_command({
                        "sender_email": "manager@example.com",
                        "subject": "Vendor registry update",
                        "body": "REMOVE VENDOR\nVendor: Alpha Plumbing",
                    })

                conn = sqlite3.connect(db_path)
                try:
                    states = dict(conn.execute(
                        "SELECT name, active FROM vendors ORDER BY name"
                    ).fetchall())
                finally:
                    conn.close()

                self.assertTrue(result)
                self.assertEqual(states["Alpha Plumbing"], 0)
                self.assertEqual(states["Alpha Plumbing North"], 1)
            finally:
                monitor.DB_PATH = original_monitor_db
                rachel_db_init.DB_PATH = original_init_db

    def test_multi_issue_partial_dispatch_restart_skips_completed_sub_wo(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            original_triage_db = triage.DB_PATH
            original_init_db = rachel_db_init.DB_PATH
            triage.DB_PATH = db_path
            rachel_db_init.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                issues = [
                    {
                        "label": "Issue One",
                        "description": "Repair one",
                        "trade": "plumbing",
                        "wo_url": (
                            "https://clientco.appfolio.com/"
                            "maintenance/service_requests/31/work_orders/3100"
                        ),
                    },
                    {
                        "label": "Issue Two",
                        "description": "Repair two",
                        "trade": "electrical",
                        "wo_url": (
                            "https://clientco.appfolio.com/"
                            "maintenance/service_requests/31/work_orders/3101"
                        ),
                    },
                ]
                conn = sqlite3.connect(db_path)
                conn.execute(
                    """
                    INSERT INTO wo_triage (
                        wo_num, wo_url, tenant, address, job_desc, issue_type,
                        conversation, issues, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, 'now', 'now')
                    """,
                    (
                        "3100-1", issues[0]["wo_url"], "Test Resident",
                        "Test Property", "Two repairs", "plumbing",
                        json.dumps(issues),
                        "waiting_for_manager_vendor_approval",
                    ),
                )
                conn.commit()
                conn.close()

                with patch.object(
                    triage,
                    "dispatch_vendor_to_wo",
                    side_effect=[True, False, True],
                ) as dispatch, patch.object(
                    triage, "_send_appfolio_message", return_value=True
                ), patch.object(
                    triage, "send_sms_via_gmail", return_value=True
                ):
                    first = triage.handle_multi_issue_dispatch(
                        "3100-1: Vendor One, 3100-2: Vendor Two", "3100"
                    )
                    second = triage.handle_multi_issue_dispatch(
                        "3100-1: Vendor One, 3100-2: Vendor Two", "3100"
                    )

                self.assertFalse(first)
                self.assertTrue(second)
                self.assertEqual(
                    [call.kwargs["wo_num"] for call in dispatch.call_args_list],
                    ["3100-1", "3100-2", "3100-2"],
                )
            finally:
                triage.DB_PATH = original_triage_db
                rachel_db_init.DB_PATH = original_init_db

    def test_multi_issue_dispatch_requires_complete_exact_sub_wo_set(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            original_triage_db = triage.DB_PATH
            original_init_db = rachel_db_init.DB_PATH
            triage.DB_PATH = db_path
            rachel_db_init.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                issues = [
                    {
                        "label": "Issue One", "description": "Repair one",
                        "trade": "plumbing",
                        "wo_url": (
                            "https://clientco.appfolio.com/"
                            "maintenance/service_requests/30/work_orders/3000"
                        ),
                    },
                    {
                        "label": "Issue Two", "description": "Repair two",
                        "trade": "electrical",
                        "wo_url": (
                            "https://clientco.appfolio.com/"
                            "maintenance/service_requests/30/work_orders/3001"
                        ),
                    },
                ]
                conn = sqlite3.connect(db_path)
                conn.execute(
                    """
                    INSERT INTO wo_triage (
                        wo_num, wo_url, tenant, address, job_desc, issue_type,
                        conversation, issues, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'now', 'now')
                    """,
                    (
                        "3000-1", issues[0]["wo_url"], "Test Resident",
                        "Test Property", "Two repairs", "plumbing", "[]",
                        json.dumps(issues),
                        "waiting_for_manager_vendor_approval",
                    ),
                )
                conn.commit()
                conn.close()

                with patch.object(
                    triage, "dispatch_vendor_to_wo", return_value=True
                ) as dispatch, patch.object(
                    triage, "_send_appfolio_message", return_value=True
                ), patch.object(
                    triage, "send_sms_via_gmail", return_value=True
                ):
                    incomplete = triage.handle_multi_issue_dispatch(
                        "3000-1: Vendor One", "3000"
                    )
                    complete = triage.handle_multi_issue_dispatch(
                        "3000-1: Vendor One, 3000-2: Vendor Two", "3000"
                    )

                self.assertFalse(incomplete)
                self.assertTrue(complete)
                self.assertEqual(dispatch.call_count, 2)
                self.assertEqual(
                    dispatch.call_args_list[0].kwargs["wo_num"], "3000-1"
                )
                self.assertEqual(
                    dispatch.call_args_list[1].kwargs["wo_num"], "3000-2"
                )
                self.assertEqual(
                    dispatch.call_args_list[1].kwargs["wo_url"],
                    issues[1]["wo_url"],
                )
            finally:
                triage.DB_PATH = original_triage_db
                rachel_db_init.DB_PATH = original_init_db

    def test_original_resident_email_is_eligible(self):
        result = triage.evaluate_triage_eligibility(details())
        self.assertTrue(result["eligible"])
        self.assertEqual(result["route"], "new_work_order_triage")

    def test_new_internal_work_order_is_not_tenant_triage_eligible(self):
        result = triage.evaluate_triage_eligibility(
            details(source_type="INTERNAL", tenant="Unknown")
        )
        self.assertFalse(result["eligible"])

        untrusted = triage.evaluate_triage_eligibility(
            details(
                triage_origin="manager_authorized",
                classification="manager_authorized_tenant_triage",
                manager_instruction="PROCEED",
                wo_url="https://example.invalid/work-orders/1700",
            )
        )
        self.assertFalse(untrusted["eligible"])
        self.assertEqual(result["route"], "internal_work_order_review")

    def test_reconciliation_is_never_triage_eligible(self):
        result = triage.evaluate_triage_eligibility(
            details(
                triage_origin="reconciliation",
                live_status="NEW",
                priority="EMERGENCY",
            )
        )
        self.assertFalse(result["eligible"])
        self.assertTrue(result["emergency"])
        self.assertEqual(result["reason"], "reconciliation_is_read_only")

    def test_synthetic_origin_label_alone_is_not_authorization(self):
        result = triage.evaluate_triage_eligibility(
            details(triage_origin="synthetic_qa")
        )
        self.assertFalse(result["eligible"])

    def test_manager_authorization_requires_instruction_and_exact_url(self):
        result = triage.evaluate_triage_eligibility(
            details(
                triage_origin="manager_authorized",
                classification="manager_authorized_tenant_triage",
                manager_instruction="",
            )
        )
        self.assertFalse(result["eligible"])

        authorized = triage.evaluate_triage_eligibility(
            details(
                triage_origin="manager_authorized",
                classification="manager_authorized_tenant_triage",
                manager_instruction="PROCEED",
            )
        )
        self.assertTrue(authorized["eligible"])
        self.assertEqual(authorized["route"], "manager_authorized_triage")

    def test_fire_safety_maintenance_is_not_active_emergency_evidence(self):
        for description in (
            "Annual fire extinguisher inspection",
            "Smoke detector inspection",
            "Replace smoke detector batteries",
            "Annual fire-safety inspection",
            "Routine fire alarm testing",
        ):
            with self.subTest(description=description):
                result = triage.detect_emergency_evidence(
                    details(job_desc=description)
                )
                self.assertFalse(result["is_emergency"])

    def test_actual_emergency_evidence_is_recognized(self):
        for payload in (
            details(priority="URGENT"),
            details(job_desc="Active fire visible in the unit"),
            details(job_desc="Visible flames in the unit"),
            details(job_desc="Resident reports smell of gas"),
            details(job_desc="Burst water pipe in kitchen"),
            details(job_desc="Active flooding from a broken supply line"),
        ):
            with self.subTest(payload=payload["job_desc"]):
                self.assertTrue(
                    triage.detect_emergency_evidence(payload)["is_emergency"]
                )

    def test_exact_current_work_order_url_is_reused(self):
        page = MagicMock()
        page.url = EXACT_URL
        page.locator.return_value.inner_text.return_value = (
            "Work Order #1700-1 NEW"
        )

        result = triage.navigate_to_wo(page, "1700-1", EXACT_URL)

        self.assertEqual(result, EXACT_URL)
        page.goto.assert_not_called()
        page.query_selector.assert_not_called()

    def test_exact_url_is_opened_directly_and_validated(self):
        page = MagicMock()
        page.url = (
            "https://clientco.appfolio.com/"
            "maintenance/service_requests/work_orders"
        )
        page.locator.return_value.inner_text.return_value = (
            "Work Order #1700-1 NEW"
        )

        def navigate(url, **_kwargs):
            page.url = url

        page.goto.side_effect = navigate
        result = triage.navigate_to_wo(page, "1700-1", EXACT_URL)

        self.assertEqual(result, EXACT_URL)
        page.goto.assert_called_once()
        page.query_selector.assert_not_called()

    def test_untrusted_url_fails_closed_without_global_search(self):
        page = MagicMock()
        page.url = "https://example.invalid/list"

        result = triage.navigate_to_wo(
            page,
            "1700-1",
            "https://example.invalid/work_orders/1700",
        )

        self.assertIsNone(result)
        page.goto.assert_not_called()
        page.query_selector.assert_not_called()

    @patch.object(triage, "send_sms_via_gmail")
    @patch.object(triage, "_send_appfolio_message")
    @patch.object(triage, "find_tenant_by_name", return_value=None)
    def test_all_live_non_new_statuses_block_contact_and_writes(
        self,
        _find_tenant,
        send_appfolio,
        send_sms,
    ):
        for status in (
            "WAITING",
            "ASSIGNED",
            "SCHEDULED",
            "WORK DONE",
            "CANCELED",
            "UNKNOWN",
        ):
            with self.subTest(status=status):
                page = MagicMock()
                page.url = EXACT_URL
                page.locator.return_value.inner_text.return_value = (
                    f"Work Order #1700-1 {status}"
                )
                result = triage.triage_work_order_details(details(), page=page)
                self.assertEqual(result["reason"], "live_status_not_new")
                page.goto.assert_not_called()

        send_appfolio.assert_not_called()
        send_sms.assert_not_called()

    def test_new_work_order_write_gate_blocks_before_any_mutating_helper(self):
        page = MagicMock()
        page.url = EXACT_URL
        page.locator.return_value.inner_text.return_value = (
            "Work Order #1700-1 NEW"
        )
        with patch.object(
            triage,
            "appfolio_write_allowed",
            return_value=(False, "RACHEL_APPFOLIO_WRITES_ENABLED=false"),
        ), patch.object(
            triage, "detect_issues"
        ) as detect_issues, patch.object(
            triage, "create_additional_work_order"
        ) as create_wo, patch.object(
            triage, "edit_wo_description"
        ) as edit_wo, patch.object(
            triage, "_send_appfolio_message"
        ) as send_message:
            result = triage.triage_work_order_details(
                details(), page=page
            )
        self.assertFalse(result)
        detect_issues.assert_not_called()
        create_wo.assert_not_called()
        edit_wo.assert_not_called()
        send_message.assert_not_called()

    @patch.object(triage, "get_appfolio_browser_manager")
    def test_missing_origin_blocks_before_browser_acquisition(self, manager):
        result = triage.triage_work_order_details(
            details(triage_origin=""),
        )
        self.assertEqual(result["status"], "triage_blocked")
        manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
