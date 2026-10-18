import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

import rachel_db_init
from rachel_lease_window_processor import (
    discover_due_lease_windows,
    process_due_lease_windows,
    run_lease_window_cycle,
)


class LeaseWindowProcessorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "rachel.db")
        original = rachel_db_init.DB_PATH
        rachel_db_init.DB_PATH = self.db_path
        try:
            rachel_db_init.initialize_database()
        finally:
            rachel_db_init.DB_PATH = original
        self.as_of = date(2026, 8, 13)

    def tearDown(self):
        self.tempdir.cleanup()

    def _tenant(
        self, occupancy_id, *, lease_type="fixed", days_out=90,
        is_active=1, excluded=False,
    ):
        target = self.as_of + timedelta(days=days_out)
        lease_end = target.isoformat() if lease_type == "fixed" else None
        increase = (
            target.isoformat() if lease_type == "month-to-month" else None
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO tenants (
                occupancy_id, property_address, primary_tenant, lease_type,
                lease_end, eligible_increase_on, is_active, past_lease_end
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                occupancy_id, "100 Oak Street", "Jamie Tenant", lease_type,
                lease_end, increase, is_active,
            ),
        )
        if excluded:
            conn.execute(
                "INSERT INTO occupancy_classifications "
                "(occupancy_id, classification, exclude_from_lease_reports, "
                "notes, updated_at) VALUES (?, 'excluded', 1, 'test', 'now')",
                (occupancy_id,),
            )
        conn.commit()
        conn.close()

    def test_exact_90_day_window_is_detected_and_not_catchup(self):
        self._tenant("occ-90", days_out=90)
        result = discover_due_lease_windows(
            db_path=self.db_path, as_of=self.as_of
        )
        self.assertEqual(result["due_events"], 1)
        self.assertEqual(result["caught_up_events"], 0)
        self.assertEqual(result["created"][0]["window_days"], 90)

    def test_downtime_catches_every_crossed_90_and_60_day_threshold(self):
        self._tenant("occ-catchup", days_out=55)
        result = discover_due_lease_windows(
            db_path=self.db_path, as_of=self.as_of
        )
        self.assertEqual(result["due_events"], 2)
        self.assertEqual(result["caught_up_events"], 2)
        self.assertEqual(
            {event["window_days"] for event in result["created"]}, {90, 60}
        )

    def test_long_downtime_past_target_still_catches_all_missed_windows(self):
        self._tenant("occ-overdue", days_out=-5)
        result = discover_due_lease_windows(
            db_path=self.db_path, as_of=self.as_of
        )
        self.assertEqual(result["due_events"], 3)
        self.assertEqual(result["caught_up_events"], 3)
        self.assertEqual(
            {event["window_days"] for event in result["created"]},
            {90, 60, 30},
        )

    def test_restart_is_idempotent_and_next_threshold_still_fires(self):
        self._tenant("occ-restart", days_out=90)
        first_notifications = []
        first = process_due_lease_windows(
            db_path=self.db_path,
            as_of=self.as_of,
            notifier=lambda subject, body: first_notifications.append(
                (subject, body)
            ) or True,
        )
        second_notifications = []
        second = process_due_lease_windows(
            db_path=self.db_path,
            as_of=self.as_of,
            notifier=lambda subject, body: second_notifications.append(
                (subject, body)
            ) or True,
        )
        third_notifications = []
        third = process_due_lease_windows(
            db_path=self.db_path,
            as_of=self.as_of + timedelta(days=30),
            notifier=lambda subject, body: third_notifications.append(
                (subject, body)
            ) or True,
        )
        self.assertEqual(first["delivered_events"], 1)
        self.assertEqual(second["due_events"], 0)
        self.assertEqual(len(second_notifications), 0)
        self.assertEqual(third["delivered_events"], 1)
        self.assertEqual(len(third_notifications), 1)
        self.assertIn("60-day", third_notifications[0][1])

    def test_ambiguous_notification_is_not_automatically_retried(self):
        self._tenant("occ-ambiguous", days_out=90)
        calls = []
        first = process_due_lease_windows(
            db_path=self.db_path,
            as_of=self.as_of,
            notifier=lambda subject, body: calls.append((subject, body)) or False,
        )
        second = process_due_lease_windows(
            db_path=self.db_path,
            as_of=self.as_of,
            notifier=lambda subject, body: calls.append((subject, body)) or True,
        )
        self.assertEqual(first["status"], "notification_unverified")
        self.assertEqual(first["notification_unverified_events"], 1)
        self.assertEqual(second["due_events"], 0)
        self.assertEqual(len(calls), 1)
        conn = sqlite3.connect(self.db_path)
        status, attempts = conn.execute(
            "SELECT status, delivery_attempts FROM lease_window_events"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "notification_unverified")
        self.assertEqual(attempts, 1)

    def test_all_three_windows_are_delivered_once_as_time_advances(self):
        self._tenant("occ-all", days_out=90)
        delivered = []
        for offset in (0, 30, 60, 61):
            process_due_lease_windows(
                db_path=self.db_path,
                as_of=self.as_of + timedelta(days=offset),
                notifier=lambda subject, body: delivered.append(body) or True,
            )
        self.assertEqual(len(delivered), 3)
        self.assertIn("90-day", delivered[0])
        self.assertIn("60-day", delivered[1])
        self.assertIn("30-day", delivered[2])
        conn = sqlite3.connect(self.db_path)
        windows = {
            row[0] for row in conn.execute(
                "SELECT window_days FROM lease_window_events "
                "WHERE status = 'notified'"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(windows, {90, 60, 30})

    def test_mtm_windows_are_processed_and_inactive_or_excluded_are_skipped(self):
        self._tenant(
            "occ-mtm", lease_type="month-to-month", days_out=60
        )
        self._tenant("occ-inactive", days_out=60, is_active=0)
        self._tenant("occ-excluded", days_out=60, excluded=True)
        result = discover_due_lease_windows(
            db_path=self.db_path, as_of=self.as_of
        )
        self.assertEqual(result["due_events"], 2)
        self.assertEqual({
            event["occupancy_id"] for event in result["created"]
        }, {"occ-mtm"})
        self.assertEqual({
            event["event_kind"] for event in result["created"]
        }, {"mtm_increase_eligible"})

    def test_dry_run_discovers_but_never_claims_or_calls_notifier(self):
        self._tenant("occ-dry-run", days_out=90)
        calls = []
        result = run_lease_window_cycle(
            db_path=self.db_path,
            as_of=self.as_of,
            notifier=lambda subject, body: calls.append((subject, body)) or True,
            delivery_enabled=False,
        )
        self.assertEqual(result["status"], "delivery_suppressed_pending")
        self.assertEqual(result["due_events"], 1)
        self.assertEqual(calls, [])
        conn = sqlite3.connect(self.db_path)
        status, attempts = conn.execute(
            "SELECT status, delivery_attempts FROM lease_window_events"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "pending_notification")
        self.assertEqual(attempts, 0)


if __name__ == "__main__":
    unittest.main()
