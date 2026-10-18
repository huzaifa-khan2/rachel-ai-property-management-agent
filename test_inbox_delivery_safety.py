import json
import importlib.util
import os
from pathlib import Path
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


import rachel_inbox_monitor as monitor
import rachel_property_review as property_review
import rachel_wo_triage as triage


def _load_lease_tracker_module():
    path = Path(__file__).with_name("rachel_lease_tracker_v2.1.py")
    spec = importlib.util.spec_from_file_location(
        "rachel_lease_tracker_delivery_test",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InboxDeliverySafetyTests(unittest.TestCase):
    def test_lease_email_is_restart_idempotent(self):
        lease_tracker = _load_lease_tracker_module()
        with tempfile.TemporaryDirectory() as root, patch.object(
            lease_tracker, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            lease_tracker, "RACHEL_EMAIL_USER", "rachel@example.com"
        ), patch.object(
            lease_tracker, "RACHEL_EMAIL_PASSWORD", "credential-present"
        ), patch.object(
            lease_tracker, "email_allowed", return_value=(True, "production")
        ), patch.dict(
            os.environ, {"RACHEL_LEASE_EMAIL_DRY_RUN": "false"}
        ), patch.object(
            lease_tracker.smtplib, "SMTP_SSL"
        ) as smtp_factory:
            smtp = smtp_factory.return_value.__enter__.return_value
            smtp.send_message.return_value = {}

            first = lease_tracker.send_email(
                "Lease alert — August 14, 2026",
                "One deterministic alert",
            )
            second = lease_tracker.send_email(
                "Lease alert — August 14, 2026",
                "One deterministic alert",
            )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(smtp_factory.call_count, 1)
        self.assertEqual(smtp.send_message.call_count, 1)

    def test_smtp_requires_a_durable_source_event_key(self):
        with patch.object(
            monitor, "email_allowed", return_value=(True, "production")
        ), patch.object(
            monitor, "RACHEL_IMAP_USER", "rachel@example.com"
        ), patch.object(
            monitor, "RACHEL_IMAP_PASS", "credential-present"
        ), patch.object(monitor.smtplib, "SMTP_SSL") as smtp:
            self.assertFalse(monitor.send_email(
                "recipient@example.com", "Subject", "Body"
            ))

        smtp.assert_not_called()

    def test_smtp_delivery_is_idempotent_for_one_inbound_event(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            monitor, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            monitor, "email_allowed", return_value=(True, "production")
        ), patch.object(
            monitor, "RACHEL_IMAP_USER", "rachel@example.com"
        ), patch.object(
            monitor, "RACHEL_IMAP_PASS", "credential-present"
        ), patch.object(
            monitor, "record_management_email_audit"
        ), patch.object(monitor.smtplib, "SMTP_SSL") as smtp_factory:
            smtp = smtp_factory.return_value.__enter__.return_value
            smtp.send_message.return_value = {}

            with monitor._outbound_delivery_event("<event@example.com>"):
                first = monitor.send_email(
                    "recipient@example.com", "Subject", "Body"
                )
            with monitor._outbound_delivery_event("<event@example.com>"):
                second = monitor.send_email(
                    "recipient@example.com", "Subject", "Body"
                )

            self.assertTrue(first)
            self.assertTrue(second)
            self.assertEqual(smtp_factory.call_count, 1)
            self.assertEqual(smtp.send_message.call_count, 1)

            db = sqlite3.connect(monitor.DB_PATH)
            try:
                status, attempts = db.execute(
                    "SELECT status, attempt_count "
                    "FROM appfolio_outbound_intents "
                    "WHERE operation = 'smtp_email'"
                ).fetchone()
            finally:
                db.close()

            self.assertEqual(status, "sent_verified")
            self.assertEqual(attempts, 1)

    def test_ambiguous_smtp_attempt_is_never_automatically_retried(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            monitor, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            monitor, "email_allowed", return_value=(True, "production")
        ), patch.object(
            monitor, "RACHEL_IMAP_USER", "rachel@example.com"
        ), patch.object(
            monitor, "RACHEL_IMAP_PASS", "credential-present"
        ), patch.object(monitor.smtplib, "SMTP_SSL") as smtp_factory:
            smtp = smtp_factory.return_value.__enter__.return_value
            smtp.send_message.side_effect = RuntimeError("connection lost")

            with monitor._outbound_delivery_event("<ambiguous@example.com>"):
                first = monitor.send_email(
                    "recipient@example.com", "Subject", "Body"
                )
            with monitor._outbound_delivery_event("<ambiguous@example.com>"):
                second = monitor.send_email(
                    "recipient@example.com", "Subject", "Body"
                )

            self.assertFalse(first)
            self.assertFalse(second)
            self.assertEqual(smtp_factory.call_count, 1)
            self.assertEqual(smtp.send_message.call_count, 1)

            db = sqlite3.connect(monitor.DB_PATH)
            try:
                status, attempts = db.execute(
                    "SELECT status, attempt_count "
                    "FROM appfolio_outbound_intents "
                    "WHERE operation = 'smtp_email'"
                ).fetchone()
            finally:
                db.close()

            self.assertEqual(status, "sending_unverified")
            self.assertEqual(attempts, 1)

    def test_property_review_sheet_apply_is_durably_deferred_by_gate(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            property_review, "DB_PATH", os.path.join(root, "rachel.db")
        ):
            case = property_review.create_or_refresh_review_case(
                property_address="300 Test Street",
                jurisdiction="Test jurisdiction",
                known_facts={},
                questions=["Confirm the property type."],
            )
            db = sqlite3.connect(property_review.DB_PATH)
            db.execute(
                "UPDATE property_review_cases SET status = ?, "
                "parsed_answers_json = ? WHERE case_id = ?",
                (
                    "ANSWERED_PENDING_APPLY",
                    json.dumps({
                        "property_updates": {
                            "Property Type": "Test property type",
                        }
                    }),
                    case["case_id"],
                ),
            )
            db.commit()
            db.close()

            with patch.object(
                property_review,
                "property_fact_sheet_write_allowed",
                return_value=(False, "independent gate disabled"),
            ), patch.object(
                property_review, "_open_rent_control_spreadsheet"
            ) as open_sheet:
                result = property_review.apply_review_answers_to_sheet(
                    case["case_id"]
                )

            self.assertFalse(result["applied"])
            self.assertTrue(result["deferred"])
            open_sheet.assert_not_called()
            self.assertEqual(
                property_review.get_review_case(case["case_id"])["status"],
                "ANSWERED_PENDING_APPLY",
            )

    def test_pending_property_review_apply_resumes_once_after_restart(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            property_review, "DB_PATH", os.path.join(root, "rachel.db")
        ):
            pending = property_review.create_or_refresh_review_case(
                property_address="400 Test Street",
                jurisdiction="Test jurisdiction",
                known_facts={},
                questions=["Confirm a pending fact."],
            )
            already_applied = property_review.create_or_refresh_review_case(
                property_address="500 Test Street",
                jurisdiction="Test jurisdiction",
                known_facts={},
                questions=["Confirm an applied fact."],
            )
            db = sqlite3.connect(property_review.DB_PATH)
            db.executemany(
                "UPDATE property_review_cases SET status = ? "
                "WHERE case_id = ?",
                [
                    ("ANSWERED_PENDING_APPLY", pending["case_id"]),
                    (
                        "CLARIFICATION_REQUIRED_APPLIED",
                        already_applied["case_id"],
                    ),
                ],
            )
            db.commit()
            db.close()

            with patch.object(
                property_review,
                "property_fact_sheet_write_allowed",
                return_value=(True, "production"),
            ), patch.object(
                property_review,
                "apply_review_answers_to_sheet",
                return_value={"applied": True},
            ) as apply_sheet:
                result = (
                    property_review.apply_pending_review_answers_to_sheet()
                )

            self.assertFalse(result["blocked"])
            apply_sheet.assert_called_once_with(pending["case_id"])

    def test_unauthorized_property_review_sender_gets_no_reply(self):
        email_data = {
            "sender_email": "outside@example.com",
            "subject": "Rachel Property Review — PR-20260814-ABC123",
            "body": "Attempted reply",
            "message_id_header": "<outside-review@example.com>",
        }
        with patch.object(
            monitor, "record_management_email_audit"
        ), patch.object(
            property_review,
            "find_review_case_from_email",
            return_value={
                "case_id": "PR-20260814-ABC123",
                "property_address": "Protected property",
            },
        ), patch.object(monitor, "send_email") as send_email:
            result = monitor.handle_manager_or_owner(email_data)

        self.assertTrue(result)
        send_email.assert_not_called()

    def test_work_order_internal_notice_checks_every_recipient_gate(self):
        smtp = MagicMock()
        with patch.object(
            triage,
            "email_allowed",
            side_effect=[(True, "production"), (False, "blocked")],
        ), patch("smtplib.SMTP_SSL", return_value=smtp):
            self.assertFalse(triage.send_sms_via_gmail("management notice"))

        smtp.login.assert_not_called()
        smtp.sendmail.assert_not_called()

    def test_work_order_internal_notice_requires_all_smtp_receipts(self):
        smtp = MagicMock()
        smtp.sendmail.side_effect = [
            {},
            {triage.MANAGER_EMAIL: (550, b"recipient refused")},
        ]
        with tempfile.TemporaryDirectory() as root, patch.object(
            triage, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            triage, "email_allowed", return_value=(True, "production")
        ), patch.object(
            triage, "RACHEL_GMAIL_PW", "credential-present"
        ), patch("smtplib.SMTP_SSL", return_value=smtp):
            self.assertFalse(triage.send_sms_via_gmail("management notice"))

        self.assertEqual(smtp.sendmail.call_count, 2)

    def test_work_order_internal_notice_is_restart_idempotent(self):
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        with tempfile.TemporaryDirectory() as root, patch.object(
            triage, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            triage, "email_allowed", return_value=(True, "production")
        ), patch.object(
            triage, "RACHEL_GMAIL_PW", "credential-present"
        ), patch("smtplib.SMTP_SSL", return_value=smtp):
            first = triage.send_sms_via_gmail(
                "WO #1000-1 — deterministic management notice"
            )
            second = triage.send_sms_via_gmail(
                "WO #1000-1 — deterministic management notice"
            )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(smtp.sendmail.call_count, 2)
        self.assertEqual(smtp.login.call_count, 1)

    def test_appfolio_sender_matching_requires_owned_domain_boundary(self):
        self.assertTrue(monitor._is_appfolio_sender("mailer@appfolio.com"))
        self.assertTrue(
            monitor._is_appfolio_sender("notice@mailer.appfolio.us")
        )
        self.assertFalse(
            monitor._is_appfolio_sender("notice@appfolio.com.attacker.test")
        )
        self.assertFalse(
            monitor._is_appfolio_sender("appfolio.com@attacker.test")
        )

    def test_management_sms_reports_success_only_when_both_channels_verify(self):
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        with tempfile.TemporaryDirectory() as root, patch.object(
            monitor, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            monitor, "sms_allowed", return_value=(True, "test")
        ), patch.object(
            monitor, "get_secret", return_value="credential-present"
        ), patch.object(
            monitor.smtplib, "SMTP_SSL", return_value=smtp
        ), patch.object(
            monitor, "send_email", return_value=True
        ) as email_send:
            self.assertTrue(monitor.send_sms(
                "verified management alert",
                delivery_key="management-alert-1",
            ))

        self.assertEqual(smtp.sendmail.call_count, 2)
        email_send.assert_called_once()

    def test_management_sms_failure_is_not_reported_as_delivery(self):
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        with tempfile.TemporaryDirectory() as root, patch.object(
            monitor, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            monitor, "sms_allowed", return_value=(True, "test")
        ), patch.object(
            monitor, "get_secret", return_value="credential-present"
        ), patch.object(
            monitor.smtplib, "SMTP_SSL", return_value=smtp
        ), patch.object(
            monitor, "send_email", return_value=False
        ):
            self.assertFalse(monitor.send_sms(
                "unverified management alert",
                delivery_key="management-alert-2",
            ))

    def test_management_sms_gateway_is_not_repeated_when_email_copy_resumes(self):
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        with tempfile.TemporaryDirectory() as root, patch.object(
            monitor, "DB_PATH", os.path.join(root, "rachel.db")
        ), patch.object(
            monitor, "sms_allowed", return_value=(True, "test")
        ), patch.object(
            monitor, "get_secret", return_value="credential-present"
        ), patch.object(
            monitor.smtplib, "SMTP_SSL", return_value=smtp
        ), patch.object(
            monitor, "send_email", side_effect=[False, True]
        ) as email_send:
            first = monitor.send_sms(
                "restart-safe management alert",
                delivery_key="management-alert-restart",
            )
            second = monitor.send_sms(
                "restart-safe management alert",
                delivery_key="management-alert-restart",
            )

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(smtp.sendmail.call_count, 2)
        self.assertEqual(email_send.call_count, 2)

    def test_external_sender_cannot_execute_vendor_registry_command(self):
        email_data = {
            "sender_email": "outside@example.com",
            "subject": "Please update",
            "body": "REMOVE VENDOR\nVendor: Trusted Plumbing",
        }
        with patch.object(
            monitor, "resolve_internal_user", return_value=None
        ), patch.object(monitor, "get_db") as get_db, patch.object(
            monitor, "send_email"
        ) as send_email:
            self.assertFalse(monitor.handle_vendor_command(email_data))

        get_db.assert_not_called()
        send_email.assert_not_called()

    def test_out_of_scope_internal_sender_cannot_execute_vendor_command(self):
        email_data = {
            "sender_email": "limited.manager@example.com",
            "subject": "Vendor update",
            "body": (
                "ADD PROPERTY VENDOR\n"
                "Property: 100 Oak Street\n"
                "Vendor: Trusted Plumbing\n"
                "Trade: plumbing"
            ),
        }
        with patch.object(
            monitor,
            "resolve_internal_user",
            return_value={"user_id": "limited-manager"},
        ), patch.object(
            monitor, "internal_user_has_access", return_value=False
        ), patch.object(monitor, "get_db") as get_db:
            self.assertFalse(monitor.handle_vendor_command(email_data))

        get_db.assert_not_called()

    def test_property_review_unverified_delivery_blocks_restart_retry(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            property_review, "DB_PATH", os.path.join(root, "rachel.db")
        ):
            case = property_review.create_or_refresh_review_case(
                property_address="100 Test Street",
                jurisdiction="Test jurisdiction",
                known_facts={},
                questions=["Confirm the test fact."],
            )
            transport = MagicMock(return_value=False)
            first = property_review.send_review_case(
                case["case_id"], transport, live_send=True
            )
            second = property_review.send_review_case(
                case["case_id"], transport, live_send=True
            )

            self.assertFalse(first["sent"])
            self.assertEqual(first["status"], "DELIVERY_UNVERIFIED")
            self.assertFalse(second["sent"])
            self.assertEqual(second["status"], "DELIVERY_UNVERIFIED")
            self.assertEqual(transport.call_count, 1)

    def test_property_review_advances_only_on_verified_delivery(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            property_review, "DB_PATH", os.path.join(root, "rachel.db")
        ):
            case = property_review.create_or_refresh_review_case(
                property_address="200 Test Street",
                jurisdiction="Test jurisdiction",
                known_facts={},
                questions=["Confirm the second test fact."],
            )
            result = property_review.send_review_case(
                case["case_id"], MagicMock(return_value=True), live_send=True
            )

            self.assertTrue(result["sent"])
            self.assertEqual(
                property_review.get_review_case(case["case_id"])["status"],
                "AWAITING_MANAGER",
            )


if __name__ == "__main__":
    unittest.main()
