import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_db_init
import rachel_vendor_lifecycle as lifecycle
from rachel_maintenance_scheduling import (
    AWAITING_TENANT_CONFIRMATION,
    AWAITING_VENDOR_WINDOW,
    capture_tenant_availability,
    get_schedule,
    record_vendor_availability_delivery,
)

# Scheduling tests exercise `require_future_two_hour_window`, so the fixture
# date must always be ahead of wall-clock time rather than a fixed literal.
_FUTURE_TEST_DATE = datetime.now() + timedelta(days=30)
FUTURE_DATE_SLASH = _FUTURE_TEST_DATE.strftime("%m/%d/%Y")
FUTURE_DATE_ISO = _FUTURE_TEST_DATE.strftime("%Y-%m-%d")


class VendorLifecycleTwoHourWindowTests(unittest.TestCase):
    def test_schedule_postcheck_requires_exact_date_start_and_two_hour_end(self):
        window = lifecycle.normalize_two_hour_window(
            date_value="08/20/2026",
            start_value="9:00 AM",
            end_value="11:00 AM",
        )
        self.assertTrue(lifecycle._schedule_window_visible_in_text(
            "Scheduled August 20, 2026 from 9:00 AM to 11:00 AM",
            window,
        ))
        self.assertTrue(lifecycle._schedule_window_visible_in_text(
            "08/20/2026 9:00 AM Duration 2 Hours",
            window,
        ))
        self.assertFalse(lifecycle._schedule_window_visible_in_text(
            "August 20, 2026 10:00 AM Duration 2 Hours",
            window,
        ))
        self.assertFalse(lifecycle._schedule_window_visible_in_text(
            "August 21, 2026 9:00 AM Duration 2 Hours",
            window,
        ))

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "rachel.db")
        self.original_db = lifecycle.DB_PATH
        self.original_init_db = rachel_db_init.DB_PATH
        lifecycle.DB_PATH = self.db_path
        rachel_db_init.DB_PATH = self.db_path
        rachel_db_init.initialize_database()
        lifecycle._ensure_lifecycle_columns()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO wo_triage (
                wo_num, wo_url, tenant, address, job_desc, issue_type,
                conversation, status, vendor_name, resident_availability,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, 'now', 'now')
            """,
            (
                "2000-1", "/work_orders/2000", "Jamie Tenant",
                "100 Oak Street", "Repair", "general",
                "waiting_for_vendor_confirmation", "Safe Vendor",
                "Monday morning",
            ),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        lifecycle.DB_PATH = self.original_db
        rachel_db_init.DB_PATH = self.original_init_db
        self.tempdir.cleanup()

    def _anthropic_response(self, payload):
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(payload))]
        )

    def test_one_hour_vendor_proposal_is_reviewed_without_tenant_send(self):
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "date": FUTURE_DATE_SLASH,
            "start": "9:00 AM",
            "end": "10:00 AM",
        })
        with patch.object(lifecycle, "_send_appfolio_message") as tenant_send, patch.object(
            lifecycle, "send_sms_via_gmail"
        ):
            lifecycle.handle_vendor_schedule_reply(
                {"body": "Available on the proposed date"}, "2000-1"
            )
        tenant_send.assert_not_called()
        conn = sqlite3.connect(self.db_path)
        status, duration = conn.execute(
            "SELECT status, proposed_duration FROM wo_triage "
            "WHERE wo_num = '2000-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "vendor_schedule_needs_review")
        self.assertIsNone(duration)

    def test_valid_vendor_window_is_sent_to_tenant_as_two_hour_window(self):
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "date": FUTURE_DATE_SLASH,
            "start": "9:00 AM",
            "end": "11:00 AM",
        })
        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ) as tenant_send, patch.object(lifecycle, "send_sms_via_gmail"):
            lifecycle.handle_vendor_schedule_reply(
                {"body": "I can arrive during the approved window"},
                "2000-1",
            )
        message = tenant_send.call_args.args[1]
        self.assertIn("two-hour arrival window", message)
        self.assertIn("9:00 AM to 11:00 AM", message)
        state = get_schedule("2000-1", db_path=self.db_path)
        self.assertEqual(state["state"], AWAITING_TENANT_CONFIRMATION)
        conn = sqlite3.connect(self.db_path)
        duration, proposed_time = conn.execute(
            "SELECT proposed_duration, proposed_time FROM wo_triage "
            "WHERE wo_num = '2000-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(duration, "2 Hours")
        self.assertEqual(proposed_time, "09:00-11:00")

    def test_explicit_tenant_confirmation_schedules_same_two_hour_window(self):
        self.test_valid_vendor_window_is_sent_to_tenant_as_two_hour_window()
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "decision": "CONFIRM", "counter_availability": None
        })
        expected_receipt = (
            "schedule_window_semantically_verified_"
            + get_schedule("2000-1", db_path=self.db_path)[
                "window_fingerprint"
            ]
        )

        def verified_schedule_write(*_args, **kwargs):
            current = get_schedule("2000-1", db_path=self.db_path)
            lifecycle.claim_appfolio_schedule_attempt(
                "2000-1",
                window_fingerprint=current["window_fingerprint"],
                attempt_id=kwargs["write_attempt_id"],
                event_key=kwargs["write_attempt_id"],
                db_path=self.db_path,
            )
            return expected_receipt

        with patch.object(
            lifecycle, "update_wo_schedule", side_effect=verified_schedule_write
        ) as schedule_write, patch.object(
            lifecycle, "send_vendor_appfolio_message", return_value=True
        ), patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ), patch.object(lifecycle, "send_sms_via_gmail"):
            result = lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Yes, I explicitly confirm that same window"
            )
        self.assertTrue(result)
        args = schedule_write.call_args.args
        self.assertEqual(args[1:5], (
            FUTURE_DATE_ISO, "09:00", "11:00", "2 Hours"
        ))
        state = get_schedule("2000-1", db_path=self.db_path)
        self.assertEqual(state["state"], "scheduled_complete")
        conn = sqlite3.connect(self.db_path)
        status, scheduled_for, duration = conn.execute(
            "SELECT status, scheduled_for, proposed_duration "
            "FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "appointment_scheduled")
        self.assertEqual(scheduled_for, f"{FUTURE_DATE_ISO} 09:00-11:00")
        self.assertEqual(duration, "2 Hours")

    def test_unclear_tenant_reply_never_reaches_schedule_write(self):
        self.test_valid_vendor_window_is_sent_to_tenant_as_two_hour_window()
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "decision": "UNCLEAR", "counter_availability": None
        })
        with patch.object(
            lifecycle, "update_wo_schedule"
        ) as schedule_write, patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ) as tenant_send, patch.object(lifecycle, "send_sms_via_gmail"):
            lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Sounds interesting"
            )
        schedule_write.assert_not_called()
        self.assertIn("explicitly confirm", tenant_send.call_args.args[1])
        self.assertEqual(
            get_schedule("2000-1", db_path=self.db_path)["state"],
            AWAITING_TENANT_CONFIRMATION,
        )

    def test_disabled_schedule_write_never_consumes_ready_window_claim(self):
        self.test_valid_vendor_window_is_sent_to_tenant_as_two_hour_window()
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "decision": "CONFIRM", "counter_availability": None
        })
        with patch.object(
            lifecycle, "TEST_MODE", True
        ), patch.object(lifecycle, "send_sms_via_gmail"):
            result = lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Yes, I explicitly confirm that same window"
            )
        self.assertFalse(result)
        state = get_schedule("2000-1", db_path=self.db_path)
        self.assertEqual(state["state"], "ready_to_schedule_two_hour_window")
        conn = sqlite3.connect(self.db_path)
        status, waiting_for = conn.execute(
            "SELECT status, waiting_for FROM wo_triage "
            "WHERE wo_num = '2000-1'"
        ).fetchone()
        claims = conn.execute(
            "SELECT COUNT(*) FROM maintenance_schedule_events "
            "WHERE event_kind = 'appfolio_schedule_write_claimed'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "schedule_write_blocked_before_claim")
        self.assertEqual(waiting_for, "appfolio_write_enablement")
        self.assertEqual(claims, 0)

    def test_pending_confirmations_retry_only_missing_audience(self):
        self.test_valid_vendor_window_is_sent_to_tenant_as_two_hour_window()
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "decision": "CONFIRM", "counter_availability": None
        })
        fingerprint = get_schedule("2000-1", db_path=self.db_path)[
            "window_fingerprint"
        ]
        receipt = "schedule_window_semantically_verified_" + fingerprint

        def verified_schedule_write(*_args, **kwargs):
            lifecycle.claim_appfolio_schedule_attempt(
                "2000-1", window_fingerprint=fingerprint,
                attempt_id=kwargs["write_attempt_id"],
                event_key=kwargs["write_attempt_id"], db_path=self.db_path,
            )
            return receipt

        with patch.object(
            lifecycle, "update_wo_schedule", side_effect=verified_schedule_write
        ), patch.object(
            lifecycle, "send_vendor_appfolio_message", return_value=True
        ) as vendor_send, patch.object(
            lifecycle, "_send_appfolio_message", return_value=False
        ) as tenant_send:
            first = lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Yes, I explicitly confirm that same window"
            )
        self.assertFalse(first)
        vendor_send.assert_called_once()
        tenant_send.assert_called_once()
        pending = get_schedule("2000-1", db_path=self.db_path)
        self.assertEqual(
            pending["state"], "scheduled_pending_final_confirmations"
        )
        self.assertTrue(pending["vendor_final_receipt"])
        self.assertFalse(pending["tenant_final_receipt"])

        lifecycle.client.messages.create.reset_mock()
        with patch.object(
            lifecycle, "send_vendor_appfolio_message"
        ) as vendor_send, patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ) as tenant_send:
            second = lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Yes, I explicitly confirm that same window"
            )
        self.assertTrue(second)
        vendor_send.assert_not_called()
        tenant_send.assert_called_once()
        lifecycle.client.messages.create.assert_not_called()
        self.assertEqual(
            get_schedule("2000-1", db_path=self.db_path)["state"],
            "scheduled_complete",
        )

        with patch.object(
            lifecycle, "send_vendor_appfolio_message"
        ) as vendor_send, patch.object(
            lifecycle, "_send_appfolio_message"
        ) as tenant_send:
            third = lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Yes, I explicitly confirm that same window"
            )
        self.assertTrue(third)
        vendor_send.assert_not_called()
        tenant_send.assert_not_called()

    def test_ready_schedule_retry_resumes_without_reclassification(self):
        self.test_valid_vendor_window_is_sent_to_tenant_as_two_hour_window()
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "decision": "CONFIRM", "counter_availability": None
        })
        with patch.object(lifecycle, "TEST_MODE", True), patch.object(
            lifecycle, "send_sms_via_gmail"
        ):
            first = lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Yes, I explicitly confirm that same window"
            )
        self.assertFalse(first)
        ready = get_schedule("2000-1", db_path=self.db_path)
        self.assertEqual(ready["state"], "ready_to_schedule_two_hour_window")
        receipt = (
            "schedule_window_semantically_verified_"
            + ready["window_fingerprint"]
        )

        def verified_schedule_write(*_args, **kwargs):
            lifecycle.claim_appfolio_schedule_attempt(
                "2000-1", window_fingerprint=ready["window_fingerprint"],
                attempt_id=kwargs["write_attempt_id"],
                event_key=kwargs["write_attempt_id"], db_path=self.db_path,
            )
            return receipt

        lifecycle.client.messages.create.reset_mock()
        with patch.object(
            lifecycle, "update_wo_schedule", side_effect=verified_schedule_write
        ), patch.object(
            lifecycle, "send_vendor_appfolio_message", return_value=True
        ), patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ):
            second = lifecycle.handle_tenant_schedule_reply(
                "2000-1", "Yes, I explicitly confirm that same window"
            )
        self.assertTrue(second)
        lifecycle.client.messages.create.assert_not_called()
        self.assertEqual(
            get_schedule("2000-1", db_path=self.db_path)["state"],
            "scheduled_complete",
        )

    def test_test_mode_schedule_helper_never_claims_success(self):
        self.assertFalse(lifecycle.update_wo_schedule(
            "/work_orders/2000", "2026-08-20", "09:00", "11:00",
            "2 Hours", wo_num="2000-1", tenant="Jamie Tenant",
            vendor_name="Safe Vendor",
        ))

    def test_vendor_followup_uses_verified_vendor_transport_not_tenant_text(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE wo_triage SET status = 'vendor_assigned', "
            "estimate_amount = NULL, updated_at = ? WHERE wo_num = '2000-1'",
            ((datetime.now() - timedelta(days=5)).isoformat(),),
        )
        conn.commit()
        conn.close()
        with patch.object(
            lifecycle, "send_vendor_appfolio_message", return_value=True
        ) as vendor_send, patch.object(
            lifecycle, "_send_appfolio_message"
        ) as tenant_send, patch.object(lifecycle, "send_sms_via_gmail"):
            lifecycle.check_vendor_followups()
        vendor_send.assert_called_once()
        self.assertEqual(vendor_send.call_args.kwargs["wo_num"], "2000-1")
        self.assertEqual(
            vendor_send.call_args.kwargs["vendor_name"], "Safe Vendor"
        )
        tenant_send.assert_not_called()

    def test_stalled_vendor_state_waits_for_verified_management_alert(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE wo_triage SET status = 'vendor_assigned', "
            "nudge_count = 2, estimate_amount = NULL, updated_at = ? "
            "WHERE wo_num = '2000-1'",
            ((datetime.now() - timedelta(days=5)).isoformat(),),
        )
        conn.commit()
        conn.close()
        with patch.object(
            lifecycle, "send_sms_via_gmail", return_value=False
        ):
            lifecycle.check_vendor_followups()
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "vendor_assigned")

        with patch.object(
            lifecycle, "send_sms_via_gmail", return_value=True
        ):
            lifecycle.check_vendor_followups()
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "vendor_stalled")

    def test_estimate_decision_advances_only_after_vendor_submission(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE wo_triage SET status = 'estimate_pending_approval', "
            "estimate_amount = 125.00 WHERE wo_num = '2000-1'"
        )
        conn.commit()
        conn.close()
        with patch.object(
            lifecycle, "send_vendor_appfolio_message", return_value=False
        ) as vendor_send, patch.object(
            lifecycle, "_send_appfolio_message"
        ) as tenant_send, patch.object(lifecycle, "send_sms_via_gmail"):
            lifecycle.handle_manager_estimate_decision("approve", "2000-1")
        vendor_send.assert_called_once()
        tenant_send.assert_not_called()
        conn = sqlite3.connect(self.db_path)
        failed_status = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(failed_status, "estimate_decision_delivery_failed")
        self.assertFalse(lifecycle.update_wo_schedule(
            "/work_orders/2000", "2026-08-20", "09:00", "10:00",
            "1 Hour", wo_num="2000-1", tenant="Jamie Tenant",
            vendor_name="Safe Vendor",
        ))

    def test_vendor_estimate_waits_for_verified_management_notification(self):
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = self._anthropic_response({
            "amount": 125.0,
            "scope": "Safe scope",
        })
        with patch.object(
            lifecycle, "send_sms_via_gmail", return_value=False
        ):
            first = lifecycle.handle_vendor_estimate(
                {"body": "Estimate details"}, "2000-1"
            )
        self.assertFalse(first)
        conn = sqlite3.connect(self.db_path)
        status, amount = conn.execute(
            "SELECT status, estimate_amount FROM wo_triage "
            "WHERE wo_num = '2000-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "waiting_for_vendor_confirmation")
        self.assertEqual(amount, 125.0)

        with patch.object(
            lifecycle, "send_sms_via_gmail", return_value=True
        ):
            second = lifecycle.handle_vendor_estimate(
                {"body": "Estimate details"}, "2000-1"
            )
        self.assertTrue(second)
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "estimate_pending_approval")

    def test_satisfaction_check_advances_only_after_verified_delivery(self):
        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=False
        ), patch.object(lifecycle, "send_sms_via_gmail"):
            first = lifecycle.send_satisfaction_check("2000-1")
        self.assertFalse(first)
        conn = sqlite3.connect(self.db_path)
        failed_status = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(
            failed_status, "satisfaction_check_delivery_failed"
        )

        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ), patch.object(lifecycle, "send_sms_via_gmail"):
            second = lifecycle.send_satisfaction_check("2000-1")
        self.assertTrue(second)
        conn = sqlite3.connect(self.db_path)
        sent_status = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(sent_status, "satisfaction_check_sent")

    def test_unsatisfied_reply_retries_each_unverified_delivery_stage(self):
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="UNSATISFIED")]
        )
        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=False
        ), patch.object(lifecycle, "send_sms_via_gmail") as management_alert:
            first = lifecycle.handle_tenant_satisfaction_reply(
                "2000-1", "The issue remains"
            )
        self.assertFalse(first)
        management_alert.assert_not_called()
        self.assertEqual(
            lifecycle._get_wo("2000-1")["status"],
            "satisfaction_response_delivery_failed",
        )

        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ), patch.object(
            lifecycle, "send_sms_via_gmail", return_value=False
        ):
            second = lifecycle.handle_tenant_satisfaction_reply(
                "2000-1", "The issue remains"
            )
        self.assertFalse(second)
        self.assertEqual(
            lifecycle._get_wo("2000-1")["status"],
            "satisfaction_escalation_delivery_failed",
        )

        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ), patch.object(
            lifecycle, "send_sms_via_gmail", return_value=True
        ):
            third = lifecycle.handle_tenant_satisfaction_reply(
                "2000-1", "The issue remains"
            )
        self.assertTrue(third)
        self.assertEqual(lifecycle._get_wo("2000-1")["status"], "reopened")

    def test_satisfied_reply_closes_only_after_verified_acknowledgement(self):
        lifecycle.client = MagicMock()
        lifecycle.client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="SATISFIED")]
        )
        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=False
        ):
            first = lifecycle.handle_tenant_satisfaction_reply(
                "2000-1", "All resolved"
            )
        self.assertFalse(first)
        self.assertEqual(
            lifecycle._get_wo("2000-1")["status"],
            "satisfaction_response_delivery_failed",
        )

        with patch.object(
            lifecycle, "_send_appfolio_message", return_value=True
        ):
            second = lifecycle.handle_tenant_satisfaction_reply(
                "2000-1", "All resolved"
            )
        self.assertTrue(second)
        self.assertEqual(
            lifecycle._get_wo("2000-1")["status"], "closed_satisfied"
        )

    def test_legacy_one_hour_scheduling_handler_is_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy scheduling handler"):
            lifecycle._legacy_handle_tenant_schedule_reply(
                "2000-1", "Tomorrow at 9"
            )

    def test_selected_duration_lookup_uses_the_centralized_selector(self):
        """
        Regression lock: AppFolio replaced the native <select> duration
        control with a react-select combobox (confirmed live Aug 28 2026),
        so update_wo_schedule reads the chosen value back from the
        centralized work_order.schedule_selected_value contract instead of
        the old, now-dead "option:checked" native-select literal. Checked
        at the source level since mocking the call in isolation wouldn't
        prove update_wo_schedule itself was changed.
        """
        import inspect

        source = inspect.getsource(lifecycle.update_wo_schedule)
        self.assertIn(
            'combined_selector("work_order.schedule_selected_value")',
            source,
        )
        self.assertNotIn("option:checked", source)
        self.assertNotIn("select_option", source)

    def test_no_raw_option_checked_literal_remains_in_module(self):
        import inspect

        source = inspect.getsource(lifecycle)
        self.assertNotIn('"option:checked"', source)
        self.assertNotIn("'option:checked'", source)


if __name__ == "__main__":
    unittest.main()
