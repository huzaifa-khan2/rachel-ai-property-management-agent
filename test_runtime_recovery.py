import os
import tempfile
import unittest
from datetime import date, datetime
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_inbox_monitor as monitor
import main as scheduler


class FakeUnreadMailbox:
    def __init__(self):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "rachel@clientco.example"
        message["Subject"] = "Old unread event"
        message["Message-ID"] = "<old-event@example.com>"
        message.set_content("Still needs processing")
        self.raw = message.as_bytes()
        self.search_calls = []
        self.store_calls = []

    def select(self, mailbox):
        self.selected = mailbox

    def search(self, charset, criterion):
        self.search_calls.append((charset, criterion))
        return "OK", [b"42"]

    def fetch(self, msg_id, query):
        return "OK", [(None, self.raw)]

    def store(self, msg_id, flags, value):
        self.store_calls.append((msg_id, flags, value))


class RuntimeRecoveryTests(unittest.TestCase):
    def test_monthly_lease_dry_run_stays_pending_without_scraping(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            as_of = date(2026, 8, 14)
            with patch.object(
                scheduler, "DB_PATH", db_path
            ), patch.object(
                scheduler, "now_pt",
                return_value=scheduler.PACIFIC.localize(
                    datetime(2026, 8, 14, 9, 0)
                ),
            ), patch.dict(
                os.environ, {"RACHEL_LEASE_EMAIL_DRY_RUN": "true"}
            ), patch.object(
                scheduler, "run_script"
            ) as run_script:
                self.assertTrue(scheduler._monthly_lease_cycle_due(
                    as_of=as_of, db_path=db_path
                ))
                self.assertFalse(scheduler.job_monthly_lease_cycle())
                self.assertTrue(scheduler._monthly_lease_cycle_due(
                    as_of=as_of, db_path=db_path
                ))

            run_script.assert_not_called()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db = monitor.DB_PATH
        monitor.DB_PATH = os.path.join(self.tempdir.name, "rachel.db")

    def tearDown(self):
        monitor.DB_PATH = self.original_db
        self.tempdir.cleanup()

    def test_unread_search_has_no_date_lookback_cap(self):
        mailbox = FakeUnreadMailbox()
        rows = monitor.fetch_unread_emails(mailbox)
        self.assertEqual(mailbox.search_calls, [(None, "UNSEEN")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id_header"], "<old-event@example.com>")

    def test_processed_marker_is_durable_before_mail_read_flag(self):
        events = []
        mailbox = MagicMock()
        mailbox.logout.return_value = None
        email_data = {
            "msg_id": b"42",
            "message_id_header": "<event@example.com>",
            "sender_email": "sender@example.com",
            "subject": "General message",
            "body": "Hello",
            "images": [],
        }
        with patch.object(
            monitor, "connect_imap", return_value=mailbox
        ), patch.object(
            monitor, "fetch_unread_emails", return_value=[email_data]
        ), patch.object(
            monitor, "_get_processed_ids", return_value=set()
        ), patch.object(
            monitor, "handle_vendor_command", return_value=False
        ), patch.object(
            monitor, "classify_email", return_value="spam"
        ), patch.object(
            monitor, "handle_spam", return_value=True
        ), patch.object(
            monitor, "_mark_processed",
            side_effect=lambda _uid: events.append("durable_processed"),
        ), patch.object(
            monitor, "mark_read",
            side_effect=lambda _mail, _id: events.append("mail_read"),
        ), patch.object(
            monitor, "_appfolio_inbox_poll_due", return_value=False
        ), patch.object(
            monitor, "_work_order_reconciliation_due", return_value=False
        ):
            monitor.run_once()
        self.assertEqual(events, ["durable_processed", "mail_read"])

    def test_inbox_failure_backoff_survives_restart(self):
        with patch.dict(
            os.environ,
            {"RACHEL_APPFOLIO_INBOX_BACKOFF_MINUTES": "15"},
            clear=False,
        ):
            monitor._mark_appfolio_inbox_poll_failed()
            self.assertFalse(monitor._appfolio_inbox_poll_due())

    def test_inbox_failure_does_not_prevent_due_reconciliation(self):
        manager = MagicMock()
        page = MagicMock()
        manager.operation.return_value.__enter__.return_value = page
        with patch.object(
            monitor, "connect_imap", return_value=None
        ), patch.object(
            monitor, "_appfolio_inbox_poll_due", return_value=True
        ), patch.object(
            monitor, "_work_order_reconciliation_due", return_value=True
        ), patch.object(
            monitor, "get_appfolio_browser_manager", return_value=manager
        ), patch.object(
            monitor, "check_appfolio_inbox", side_effect=RuntimeError("poll")
        ), patch.object(
            monitor, "_mark_appfolio_inbox_poll_failed"
        ) as mark_inbox_failed, patch.object(
            monitor, "init_scanner_state_table"
        ), patch.object(
            monitor, "reconcile_logged_in_page",
            return_value={"workflow_actions_attempted": 0},
        ) as reconcile, patch.object(
            monitor, "_mark_work_order_reconciliation_completed"
        ) as mark_reconcile_complete:
            monitor.run_once()
        mark_inbox_failed.assert_called_once_with()
        reconcile.assert_called_once()
        self.assertIs(reconcile.call_args.args[0], page)
        self.assertIsNotNone(
            reconcile.call_args.kwargs.get("navigation_metrics_provider")
        )
        mark_reconcile_complete.assert_called_once_with()

    def test_monthly_cycle_catches_up_after_scheduled_day_and_is_durable(self):
        db_path = os.path.join(self.tempdir.name, "monthly.db")
        after_first_business_day = date(2026, 8, 10)
        self.assertTrue(scheduler._monthly_lease_cycle_due(
            as_of=after_first_business_day, db_path=db_path
        ))
        scheduler._mark_monthly_lease_cycle_completed(
            as_of=after_first_business_day, db_path=db_path
        )
        self.assertFalse(scheduler._monthly_lease_cycle_due(
            as_of=date(2026, 8, 31), db_path=db_path
        ))
        self.assertTrue(scheduler._monthly_lease_cycle_due(
            as_of=date(2026, 9, 1), db_path=db_path
        ))

    def test_scheduler_startup_invokes_durable_lease_window_catchup(self):
        with open(scheduler.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        startup = source[source.index("def run_scheduler():") :]
        monthly = startup.index("job_monthly_lease_cycle()")
        daily = startup.index("job_daily_lease_windows()", monthly)
        loop = startup.index(
            "while True:\n        schedule.run_pending()",
            daily,
        )
        self.assertLess(monthly, daily)
        self.assertLess(daily, loop)


if __name__ == "__main__":
    unittest.main()
