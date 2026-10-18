import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from rachel_maintenance_scheduling import (
    AWAITING_TENANT_CONFIRMATION,
    AWAITING_VENDOR_WINDOW,
    READY_TO_SCHEDULE,
    SCHEDULED_COMPLETE,
    SCHEDULED_PENDING_CONFIRMATIONS,
    TENANT_AVAILABILITY_RECEIVED,
    ScheduleWorkflowError,
    build_tenant_window_confirmation_request,
    build_vendor_window_request,
    capture_tenant_availability,
    claim_appfolio_schedule_attempt,
    get_schedule,
    initialize_schedule,
    normalize_two_hour_window,
    record_appfolio_schedule_verified,
    record_final_confirmation_delivery,
    record_tenant_window_confirmation,
    record_vendor_availability_delivery,
    record_vendor_two_hour_window,
    verify_appfolio_schedule_attempt_claim,
)


class MaintenanceSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "rachel.db")
        self.wo_num = "2000-1"
        self.reference_time = datetime(
            2026, 8, 13, 12, 0,
            tzinfo=ZoneInfo("America/Los_Angeles"),
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _through_vendor_window(self):
        capture_tenant_availability(
            self.wo_num,
            "Monday morning or Tuesday afternoon",
            event_key="tenant-availability-1",
            db_path=self.db_path,
        )
        record_vendor_availability_delivery(
            self.wo_num,
            delivery_receipt="vendor-delivery-1",
            event_key="availability-delivered-1",
            db_path=self.db_path,
        )
        return record_vendor_two_hour_window(
            self.wo_num,
            date_value="08/20/2026",
            start_value="9:00 AM",
            end_value="11:00 AM",
            event_key="vendor-window-1",
            db_path=self.db_path,
            reference_time=self.reference_time,
        )

    def _through_schedule(self):
        window = self._through_vendor_window()
        ready = record_tenant_window_confirmation(
            self.wo_num,
            confirmed_window_fingerprint=window["window_fingerprint"],
            explicitly_confirmed=True,
            event_key="tenant-confirmation-1",
            db_path=self.db_path,
            reference_time=self.reference_time,
        )
        claim_appfolio_schedule_attempt(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            attempt_id="write-attempt-1",
            event_key="write-attempt-1",
            db_path=self.db_path,
        )
        return record_appfolio_schedule_verified(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            appfolio_write_receipt=(
                "schedule_window_semantically_verified_"
                + ready["window_fingerprint"]
            ),
            event_key="schedule-write-1",
            db_path=self.db_path,
        )

    def test_normalizes_only_exact_same_day_two_hour_windows(self):
        window = normalize_two_hour_window(
            date_value="August 20, 2026",
            start_value="9 AM",
            end_value="11:00 AM",
        )
        self.assertEqual(window["date"], "2026-08-20")
        self.assertEqual(window["start"], "09:00")
        self.assertEqual(window["end"], "11:00")
        self.assertEqual(window["duration_minutes"], 120)
        self.assertIn("9:00 AM to 11:00 AM", window["display"])

    def test_one_hour_broad_reversed_and_ambiguous_windows_fail_closed(self):
        cases = (
            ("08/20/2026", "9 AM", "10 AM", "exactly_two_hours"),
            ("08/20/2026", "morning", "11 AM", "time_invalid"),
            ("08/20/2026", "11 AM", "9 AM", "reversed"),
            ("next Tuesday", "9 AM", "11 AM", "date_invalid"),
        )
        for date_value, start, end, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ScheduleWorkflowError):
                    normalize_two_hour_window(
                        date_value=date_value,
                        start_value=start,
                        end_value=end,
                    )

    def test_past_two_hour_window_cannot_enter_scheduling_state(self):
        capture_tenant_availability(
            self.wo_num,
            "Monday morning",
            event_key="tenant-availability-past",
            db_path=self.db_path,
        )
        record_vendor_availability_delivery(
            self.wo_num,
            delivery_receipt="vendor-delivery-past",
            event_key="availability-delivered-past",
            db_path=self.db_path,
        )
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "window_start_must_be_future"
        ):
            record_vendor_two_hour_window(
                self.wo_num,
                date_value="08/12/2026",
                start_value="9:00 AM",
                end_value="11:00 AM",
                event_key="vendor-window-past",
                db_path=self.db_path,
                reference_time=self.reference_time,
            )
        self.assertEqual(
            get_schedule(self.wo_num, db_path=self.db_path)["state"],
            AWAITING_VENDOR_WINDOW,
        )

    def test_required_sequence_reaches_ready_only_after_same_window_confirmation(self):
        initial = initialize_schedule(self.wo_num, db_path=self.db_path)
        self.assertEqual(initial["state"], "awaiting_tenant_availability")
        availability = capture_tenant_availability(
            self.wo_num,
            "Monday morning",
            event_key="tenant-availability-1",
            db_path=self.db_path,
        )
        self.assertEqual(availability["state"], TENANT_AVAILABILITY_RECEIVED)
        delivered = record_vendor_availability_delivery(
            self.wo_num,
            delivery_receipt="vendor-delivery-1",
            event_key="availability-delivered-1",
            db_path=self.db_path,
        )
        self.assertEqual(delivered["state"], AWAITING_VENDOR_WINDOW)
        window = record_vendor_two_hour_window(
            self.wo_num,
            date_value="2026-08-20",
            start_value="13:00",
            end_value="15:00",
            event_key="vendor-window-1",
            db_path=self.db_path,
            reference_time=self.reference_time,
        )
        self.assertEqual(window["state"], AWAITING_TENANT_CONFIRMATION)
        ready = record_tenant_window_confirmation(
            self.wo_num,
            confirmed_window_fingerprint=window["window_fingerprint"],
            explicitly_confirmed=True,
            event_key="tenant-confirmation-1",
            db_path=self.db_path,
            reference_time=self.reference_time,
        )
        self.assertEqual(ready["state"], READY_TO_SCHEDULE)

    def test_vendor_window_cannot_precede_delivery_of_tenant_availability(self):
        initialize_schedule(self.wo_num, db_path=self.db_path)
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "vendor_two_hour_window_agreed_not_allowed"
        ):
            record_vendor_two_hour_window(
                self.wo_num,
                date_value="08/20/2026",
                start_value="9 AM",
                end_value="11 AM",
                event_key="out-of-order",
                db_path=self.db_path,
                reference_time=self.reference_time,
            )

    def test_tenant_must_explicitly_confirm_exact_vendor_window(self):
        window = self._through_vendor_window()
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "confirmation_not_explicit"
        ):
            record_tenant_window_confirmation(
                self.wo_num,
                confirmed_window_fingerprint=window["window_fingerprint"],
                explicitly_confirmed=False,
                event_key="vague-reply",
                db_path=self.db_path,
                reference_time=self.reference_time,
            )
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "different_or_unknown_window"
        ):
            record_tenant_window_confirmation(
                self.wo_num,
                confirmed_window_fingerprint="another-window",
                explicitly_confirmed=True,
                event_key="wrong-window",
                db_path=self.db_path,
            )
        self.assertEqual(
            get_schedule(self.wo_num, db_path=self.db_path)["state"],
            AWAITING_TENANT_CONFIRMATION,
        )

    def test_tenant_cannot_confirm_an_expired_vendor_window(self):
        window = self._through_vendor_window()
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "window_start_must_be_future"
        ):
            record_tenant_window_confirmation(
                self.wo_num,
                confirmed_window_fingerprint=window["window_fingerprint"],
                explicitly_confirmed=True,
                event_key="late-tenant-confirmation",
                db_path=self.db_path,
                reference_time=datetime(
                    2026, 8, 20, 9, 1,
                    tzinfo=ZoneInfo("America/Los_Angeles"),
                ),
            )
        self.assertEqual(
            get_schedule(self.wo_num, db_path=self.db_path)["state"],
            AWAITING_TENANT_CONFIRMATION,
        )

    def test_appfolio_write_requires_ready_state_same_window_and_receipt(self):
        window = self._through_vendor_window()
        with self.assertRaises(ScheduleWorkflowError):
            record_appfolio_schedule_verified(
                self.wo_num,
                window_fingerprint=window["window_fingerprint"],
                appfolio_write_receipt="verified-write-early",
                event_key="early-write",
                db_path=self.db_path,
            )
        ready = record_tenant_window_confirmation(
            self.wo_num,
            confirmed_window_fingerprint=window["window_fingerprint"],
            explicitly_confirmed=True,
            event_key="tenant-confirmation-1",
            db_path=self.db_path,
            reference_time=self.reference_time,
        )
        with self.assertRaisesRegex(ScheduleWorkflowError, "window_mismatch"):
            record_appfolio_schedule_verified(
                self.wo_num,
                window_fingerprint="wrong",
                appfolio_write_receipt="verified-write-1",
                event_key="wrong-window-write",
                db_path=self.db_path,
            )
        claim_appfolio_schedule_attempt(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            attempt_id="write-attempt-1",
            event_key="write-attempt-1",
            db_path=self.db_path,
        )
        scheduled = record_appfolio_schedule_verified(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            appfolio_write_receipt=(
                "schedule_window_semantically_verified_"
                + ready["window_fingerprint"]
            ),
            event_key="schedule-write-1",
            db_path=self.db_path,
        )
        self.assertEqual(scheduled["state"], SCHEDULED_PENDING_CONFIRMATIONS)

    def test_both_final_delivery_receipts_are_required_for_complete(self):
        scheduled = self._through_schedule()
        vendor = record_final_confirmation_delivery(
            self.wo_num,
            audience="vendor",
            delivery_receipt="vendor-confirmation-1",
            event_key="vendor-final-1",
            db_path=self.db_path,
        )
        self.assertEqual(vendor["state"], SCHEDULED_PENDING_CONFIRMATIONS)
        tenant = record_final_confirmation_delivery(
            self.wo_num,
            audience="tenant",
            delivery_receipt="tenant-confirmation-1",
            event_key="tenant-final-1",
            db_path=self.db_path,
        )
        self.assertEqual(tenant["state"], SCHEDULED_COMPLETE)
        self.assertTrue(tenant["vendor_final_receipt"])
        self.assertTrue(tenant["tenant_final_receipt"])

    def test_events_are_idempotent_across_restart(self):
        first = capture_tenant_availability(
            self.wo_num,
            "Monday morning",
            event_key="tenant-availability-1",
            db_path=self.db_path,
        )
        second = capture_tenant_availability(
            self.wo_num,
            "Monday morning",
            event_key="tenant-availability-1",
            db_path=self.db_path,
        )
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["revision"], second["revision"])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM maintenance_schedule_events"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_reused_event_key_with_different_payload_fails_closed(self):
        capture_tenant_availability(
            self.wo_num,
            "Monday morning",
            event_key="tenant-availability-1",
            db_path=self.db_path,
        )
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "idempotency_conflict"
        ):
            capture_tenant_availability(
                self.wo_num,
                "Different availability",
                event_key="tenant-availability-1",
                db_path=self.db_path,
            )
        self.assertEqual(
            get_schedule(self.wo_num, db_path=self.db_path)[
                "resident_availability"
            ],
            "Monday morning",
        )

    def test_claimed_schedule_write_is_not_automatically_retryable(self):
        window = self._through_vendor_window()
        ready = record_tenant_window_confirmation(
            self.wo_num,
            confirmed_window_fingerprint=window["window_fingerprint"],
            explicitly_confirmed=True,
            event_key="tenant-confirmation-1",
            db_path=self.db_path,
            reference_time=self.reference_time,
        )
        first = claim_appfolio_schedule_attempt(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            attempt_id="write-attempt-ambiguous",
            event_key="write-attempt-ambiguous",
            db_path=self.db_path,
        )
        second = claim_appfolio_schedule_attempt(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            attempt_id="write-attempt-ambiguous",
            event_key="write-attempt-ambiguous",
            db_path=self.db_path,
        )
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "write_claimed_not_allowed"
        ):
            claim_appfolio_schedule_attempt(
                self.wo_num,
                window_fingerprint=ready["window_fingerprint"],
                attempt_id="different-retry",
                event_key="different-retry",
                db_path=self.db_path,
            )

    def test_browser_write_claim_is_bound_to_exact_attempt_and_window(self):
        window = self._through_vendor_window()
        ready = record_tenant_window_confirmation(
            self.wo_num,
            confirmed_window_fingerprint=window["window_fingerprint"],
            explicitly_confirmed=True,
            event_key="tenant-confirmation-claim",
            db_path=self.db_path,
            reference_time=self.reference_time,
        )
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "write_not_claimed"
        ):
            verify_appfolio_schedule_attempt_claim(
                self.wo_num,
                window_fingerprint=ready["window_fingerprint"],
                attempt_id="exact-attempt",
                db_path=self.db_path,
            )
        claim_appfolio_schedule_attempt(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            attempt_id="exact-attempt",
            event_key="exact-attempt",
            db_path=self.db_path,
        )
        verified = verify_appfolio_schedule_attempt_claim(
            self.wo_num,
            window_fingerprint=ready["window_fingerprint"],
            attempt_id="exact-attempt",
            db_path=self.db_path,
        )
        self.assertEqual(
            verified["window_fingerprint"], ready["window_fingerprint"]
        )
        with self.assertRaisesRegex(
            ScheduleWorkflowError, "attempt_mismatch"
        ):
            verify_appfolio_schedule_attempt_claim(
                self.wo_num,
                window_fingerprint=ready["window_fingerprint"],
                attempt_id="another-attempt",
                db_path=self.db_path,
            )

    def test_messages_require_two_hour_language_not_exact_appointment(self):
        vendor = build_vendor_window_request(
            self.wo_num, "Monday morning"
        )
        self.assertIn("two-hour arrival window", vendor)
        self.assertIn("Do not provide an exact appointment time", vendor)
        state = self._through_vendor_window()
        tenant = build_tenant_window_confirmation_request(
            self.wo_num, state
        )
        self.assertIn("two-hour arrival window", tenant)
        self.assertIn("9:00 AM to 11:00 AM", tenant)
        self.assertNotIn("exact appointment", tenant.lower())


if __name__ == "__main__":
    unittest.main()
