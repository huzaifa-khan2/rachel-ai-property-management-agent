import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Scheduling tests exercise `require_future_two_hour_window`, so the fixture
# date must always be ahead of wall-clock time rather than a fixed literal.
_FUTURE_TEST_DATE = datetime.now() + timedelta(days=30)
FUTURE_DATE_SLASH = _FUTURE_TEST_DATE.strftime("%m/%d/%Y")
FUTURE_DATE_LONG = _FUTURE_TEST_DATE.strftime("%B %-d, %Y")

for key in (
    "ANTHROPIC_API_KEY",
    "APPFOLIO_EMAIL",
    "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER",
    "RACHEL_GMAIL_PASSWORD",
    "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_safety as safety
import rachel_vendor_lifecycle as vendor_lifecycle
import rachel_wo_triage as triage
from rachel_appfolio_write_intents import (
    claim_outbound_intent,
    complete_outbound_intent,
    prepare_outbound_intent,
)


class FakeElement:
    def __init__(self):
        self.value = ""
        self.clicked = 0
        self.checked = False

    def is_visible(self):
        return True

    def fill(self, value):
        self.value = value

    def input_value(self):
        return self.value

    def click(self, **_kwargs):
        self.clicked += 1

    def check(self, **_kwargs):
        self.checked = True


class FakeTwoFactorPage:
    def __init__(self):
        self.url = "https://example.appfolio.com/login"
        self.email = FakeElement()
        self.password = FakeElement()
        self.submit = FakeElement()
        self.radio = FakeElement()
        self.send_code = FakeElement()
        self.code = FakeElement()
        self.body_reads = 0

    def evaluate(self, script, *_args):
        if "document.body.innerText" in script:
            self.body_reads += 1
            if self.body_reads <= 2:
                return "Log in"
            if self.body_reads == 3:
                return "Send verification code"
            return "Dashboard"
        if "radio && radio.checked" in script:
            return self.radio.checked
        return True

    def query_selector(self, selector):
        if selector == 'input[type="email"]':
            return self.email
        if selector == 'input[type="password"]':
            return self.password
        if selector == 'input[type="radio"][name="twoFactorMethod"]':
            return self.radio
        if '[value="sms"]' in selector:
            return self.radio
        if "Send Verification Code" in selector:
            return self.send_code
        if selector == 'input[type="text"]':
            return self.code
        if selector == 'button[type="submit"]':
            return self.submit
        return None

    def wait_for_load_state(self, **_kwargs):
        return None


class LoginAndSafetyTests(unittest.TestCase):
    @patch.object(triage.time, "sleep", return_value=None)
    @patch.object(triage, "get_2fa_code", return_value="123456")
    def test_2fa_sms_path_is_detected_and_verified(self, get_code, _sleep):
        page = FakeTwoFactorPage()

        self.assertTrue(triage.login_appfolio(page))

        get_code.assert_called_once_with(max_wait=90)
        self.assertTrue(page.radio.checked)
        self.assertEqual(page.send_code.clicked, 1)
        self.assertEqual(page.code.value, "123456")
        self.assertGreaterEqual(page.submit.clicked, 2)

    def test_central_safety_policy_fails_closed(self):
        with patch.multiple(
            safety,
            RACHEL_ENABLED=False,
            RACHEL_TEST_MODE=False,
        ):
            self.assertFalse(safety.appfolio_write_allowed()[0])
            self.assertFalse(safety.vendor_contact_allowed()[0])
            self.assertFalse(safety.email_allowed("owner@example.com")[0])
            self.assertFalse(safety.sms_allowed()[0])

        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=False,
            RACHEL_APPFOLIO_WRITES_ENABLED=False,
        ):
            self.assertFalse(
                safety.appfolio_write_allowed(synthetic_qa=True)[0]
            )
            self.assertFalse(
                safety.vendor_contact_allowed(synthetic_qa=True)[0]
            )
            self.assertFalse(safety.appfolio_write_allowed()[0])
            self.assertFalse(safety.vendor_contact_allowed()[0])

        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=False,
            RACHEL_APPFOLIO_WRITES_ENABLED=True,
        ):
            self.assertTrue(safety.appfolio_write_allowed()[0])
            self.assertTrue(safety.vendor_contact_allowed()[0])

    def test_property_fact_sheet_writes_require_separate_production_gate(self):
        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=True,
            RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED=True,
        ):
            self.assertFalse(
                safety.property_fact_sheet_write_allowed()[0]
            )
        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=False,
            RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED=False,
        ):
            self.assertFalse(
                safety.property_fact_sheet_write_allowed()[0]
            )
        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=False,
            RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED=True,
        ):
            self.assertTrue(
                safety.property_fact_sheet_write_allowed()[0]
            )

    def test_communication_send_qa_is_exact_selector_and_authorization_bound(self):
        selector = "a" * 64
        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=True,
            RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED=True,
            RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_SELECTOR_KEY=selector,
            RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_AUTHORIZATION_ID="approval-1",
        ):
            self.assertTrue(
                safety.appfolio_communication_send_allowed(
                    selector, "approval-1"
                )[0]
            )
            self.assertFalse(
                safety.appfolio_communication_send_allowed(
                    "b" * 64, "approval-1"
                )[0]
            )
            self.assertFalse(
                safety.appfolio_communication_send_allowed(
                    selector, "different-approval"
                )[0]
            )

    def test_production_communication_send_requires_both_write_gates(self):
        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=False,
            RACHEL_APPFOLIO_WRITES_ENABLED=True,
            RACHEL_APPFOLIO_MESSAGING_ENABLED=False,
        ):
            self.assertFalse(safety.appfolio_communication_send_allowed()[0])
        with patch.multiple(
            safety,
            RACHEL_ENABLED=True,
            RACHEL_TEST_MODE=False,
            RACHEL_APPFOLIO_WRITES_ENABLED=True,
            RACHEL_APPFOLIO_MESSAGING_ENABLED=True,
        ):
            self.assertTrue(safety.appfolio_communication_send_allowed()[0])

    def test_controlled_qa_requires_exact_allowlisted_target(self):
        with patch.multiple(
            safety,
            RACHEL_APPFOLIO_QA_ENABLED=True,
            RACHEL_APPFOLIO_QA_WO_NUM="1700-1",
            RACHEL_APPFOLIO_QA_WO_ID="9001",
            RACHEL_APPFOLIO_QA_TENANT="jamie tenant",
            RACHEL_APPFOLIO_QA_VENDOR="vendor one",
        ):
            allowed = safety.controlled_appfolio_qa_allowed(
                operation="dispatch_vendor",
                wo_num="1700-1",
                wo_url="https://example.appfolio.com/work_orders/9001",
                tenant="Jamie Tenant",
                vendor_name="Vendor One",
            )
            wrong_vendor = safety.controlled_appfolio_qa_allowed(
                operation="dispatch_vendor",
                wo_num="1700-1",
                wo_url="https://example.appfolio.com/work_orders/9001",
                tenant="Jamie Tenant",
                vendor_name="Vendor Two",
            )

        self.assertTrue(allowed[0])
        self.assertFalse(wrong_vendor[0])

    def test_vendor_dispatch_gate_blocks_before_browser_acquisition(self):
        with patch.object(
            triage,
            "vendor_contact_allowed",
            return_value=(False, "blocked for deterministic test"),
        ), patch.object(
            triage,
            "appfolio_write_allowed",
            return_value=(True, "allowed"),
        ), patch.object(
            triage, "get_appfolio_browser_manager"
        ) as get_manager:
            result = triage.dispatch_vendor_to_wo(
                "https://example.appfolio.com/work_orders/9001",
                "1700-1",
                "Vendor One",
                "555-0100",
                "plumbing",
                "Jamie Tenant",
                "100 Oak Street",
            )

        self.assertFalse(result)
        get_manager.assert_not_called()

    def test_vendor_dispatch_never_retries_ambiguous_durable_intent(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            message = {
                "address": "100 Oak Street",
                "notifications": [
                    "email_secure_work_order_link",
                    "text_secure_work_order_link",
                    "request_vendor_receipt_confirmation",
                ],
                "tenant": "Jamie Tenant",
                "trade": "plumbing",
                "vendor": "Vendor One",
            }
            intent = prepare_outbound_intent(
                operation="dispatch_vendor",
                target_id="1700-1",
                recipient="Vendor One",
                message=__import__("json").dumps(message, sort_keys=True),
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)

            with patch.object(
                triage, "DB_PATH", db_path
            ), patch.object(
                triage, "vendor_contact_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "get_appfolio_browser_manager"
            ) as manager:
                result = triage.dispatch_vendor_to_wo(
                    "https://example.appfolio.com/work_orders/9001",
                    "1700-1", "Vendor One", "555-0100", "plumbing",
                    "Jamie Tenant", "100 Oak Street",
                )

            self.assertFalse(result)
            manager.assert_not_called()

    def test_verified_vendor_dispatch_duplicate_skips_browser(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            message = {
                "address": "100 Oak Street",
                "notifications": [
                    "email_secure_work_order_link",
                    "text_secure_work_order_link",
                    "request_vendor_receipt_confirmation",
                ],
                "tenant": "Jamie Tenant",
                "trade": "plumbing",
                "vendor": "Vendor One",
            }
            intent = prepare_outbound_intent(
                operation="dispatch_vendor",
                target_id="1700-1",
                recipient="Vendor One",
                message=__import__("json").dumps(message, sort_keys=True),
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)
            complete_outbound_intent(
                intent["intent_id"],
                verification_receipt="verified-test-assignment",
                db_path=db_path,
            )

            with patch.object(
                triage, "DB_PATH", db_path
            ), patch.object(
                triage, "vendor_contact_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "get_appfolio_browser_manager"
            ) as manager:
                result = triage.dispatch_vendor_to_wo(
                    "https://example.appfolio.com/work_orders/9001",
                    "1700-1", "Vendor One", "555-0100", "plumbing",
                    "Jamie Tenant", "100 Oak Street",
                )

            self.assertTrue(result)
            manager.assert_not_called()

    def test_legacy_work_order_helpers_gate_before_any_page_mutation(self):
        page = MagicMock()
        with patch.object(
            triage,
            "appfolio_write_allowed",
            return_value=(False, "release candidate write gate disabled"),
        ):
            self.assertFalse(
                triage.send_tenant_email_via_appfolio(page, "safe message")
            )
            self.assertFalse(
                triage.create_additional_work_order(
                    page, "Safe issue", "Safe description"
                )
            )
            self.assertFalse(
                triage.edit_wo_description(
                    page,
                    "https://example.appfolio.com/work_orders/1",
                    "Safe description",
                )
            )
        page.evaluate.assert_not_called()
        page.query_selector.assert_not_called()
        page.goto.assert_not_called()

    def test_verified_additional_wo_restart_restores_exact_created_url(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            service_request_url = (
                "https://clientco.appfolio.com/"
                "maintenance/service_requests/30"
            )
            created_url = service_request_url + "/work_orders/3001"
            intent = prepare_outbound_intent(
                operation="create_additional_work_order",
                target_id=service_request_url,
                recipient="appfolio_work_order_record",
                message=(
                    '{"issue_label": "Second issue", '
                    '"job_description": "Repair second issue"}'
                ),
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)
            complete_outbound_intent(
                intent["intent_id"],
                verification_receipt="verified_new_work_order_url:" + created_url,
                db_path=db_path,
            )
            page = MagicMock()
            page.url = service_request_url
            with patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ):
                result = triage.create_additional_work_order(
                    page, "Second issue", "Repair second issue",
                    db_path=db_path,
                )
            self.assertEqual(result, created_url)
            page.get_by_text.assert_not_called()

    def test_vendor_message_never_retries_ambiguous_durable_intent(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            intent = prepare_outbound_intent(
                operation="vendor_schedule_message",
                target_id="1700-1",
                recipient="Vendor One",
                message="Safe vendor message",
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)
            with patch.object(
                triage, "DB_PATH", db_path
            ), patch.object(
                triage, "vendor_contact_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "get_appfolio_browser_manager"
            ) as manager:
                result = triage.send_vendor_appfolio_message(
                    "https://example.appfolio.com/work_orders/9001",
                    "Safe vendor message",
                    wo_num="1700-1",
                    tenant="Jamie Tenant",
                    vendor_name="Vendor One",
                )
            self.assertFalse(result)
            manager.assert_not_called()

    def test_vendor_message_suppresses_already_verified_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            intent = prepare_outbound_intent(
                operation="vendor_schedule_message",
                target_id="1700-1",
                recipient="Vendor One",
                message="Safe verified message",
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)
            complete_outbound_intent(
                intent["intent_id"],
                verification_receipt="verified-test-receipt",
                db_path=db_path,
            )
            with patch.object(
                triage, "DB_PATH", db_path
            ), patch.object(
                triage, "vendor_contact_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "get_appfolio_browser_manager"
            ) as manager:
                result = triage.send_vendor_appfolio_message(
                    "https://example.appfolio.com/work_orders/9001",
                    "Safe verified message",
                    wo_num="1700-1",
                    tenant="Jamie Tenant",
                    vendor_name="Vendor One",
                )
            self.assertTrue(result)
            manager.assert_not_called()

    def test_tenant_message_never_retries_ambiguous_durable_intent(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            intent = prepare_outbound_intent(
                operation="tenant_work_order_text",
                target_id="1700-1",
                recipient="Jamie Tenant",
                message="Safe tenant message",
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)
            with patch.object(
                triage, "DB_PATH", db_path
            ), patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "get_appfolio_browser_manager"
            ) as manager:
                result = triage._send_appfolio_message(
                    "https://example.appfolio.com/work_orders/9001",
                    "Safe tenant message",
                    "Jamie Tenant",
                    wo_num="1700-1",
                )
            self.assertFalse(result)
            manager.assert_not_called()

    def test_tenant_message_suppresses_verified_duplicate_before_browser(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            intent = prepare_outbound_intent(
                operation="tenant_work_order_text",
                target_id="1700-1",
                recipient="Jamie Tenant",
                message="Safe verified tenant message",
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)
            complete_outbound_intent(
                intent["intent_id"],
                verification_receipt="verified-test-receipt",
                db_path=db_path,
            )
            with patch.object(
                triage, "DB_PATH", db_path
            ), patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "get_appfolio_browser_manager"
            ) as manager:
                result = triage._send_appfolio_message(
                    "https://example.appfolio.com/work_orders/9001",
                    "Safe verified tenant message",
                    "Jamie Tenant",
                    wo_num="1700-1",
                )
            self.assertTrue(result)
            manager.assert_not_called()

    def test_tenant_email_ambiguous_intent_blocks_before_page_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            intent = prepare_outbound_intent(
                operation="tenant_work_order_email",
                target_id="1700-1",
                recipient="Jamie Tenant",
                message="Safe tenant email",
                db_path=db_path,
            )
            claim_outbound_intent(intent["intent_id"], db_path=db_path)
            page = MagicMock()
            with patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "_validate_current_work_order_page", return_value=True
            ), patch.object(
                triage, "AppFolioReadAdapter"
            ):
                result = triage.send_tenant_email_via_appfolio(
                    page,
                    "Safe tenant email",
                    wo_url="https://example.appfolio.com/work_orders/9001",
                    wo_num="1700-1",
                    tenant="Jamie Tenant",
                    db_path=db_path,
                )
            self.assertFalse(result)
            page.evaluate.assert_not_called()

    def _mock_notify_tenant_email_form(
        self, *, existing_bcc_value: str, updated_bcc_value: str = ""
    ):
        """Build the page/email_form mock chain used by both Manager-BCC tests."""
        page = MagicMock()
        actions_item = MagicMock()
        actions_item.is_visible.return_value = True
        actions_item.element_handle.return_value = None
        actions_collection = MagicMock()
        actions_collection.count.return_value = 1
        actions_collection.nth.return_value = actions_item

        notify_item = MagicMock()
        notify_item.is_visible.return_value = True
        notify_item.element_handle.return_value = None
        notify_collection = MagicMock()
        notify_collection.count.return_value = 1
        notify_collection.nth.return_value = notify_item

        empty = MagicMock()
        empty.count.return_value = 0

        body_field = MagicMock()
        body_field.count.return_value = 1
        body_field.is_visible.return_value = True
        body_field.input_value.return_value = "Safe tenant email"

        email_form = MagicMock()
        email_form.count.return_value = 1
        email_form.inner_text.return_value = (
            "Recipients Jamie Tenant Email subject 1700-1 service request"
        )
        body_field.locator.return_value = email_form

        subject = MagicMock()
        subject.count.return_value = 1
        subject.is_visible.return_value = True
        subject.input_value.return_value = "1700-1 service request"

        send = MagicMock()
        send.count.return_value = 1
        send.is_visible.return_value = True
        send.is_disabled.return_value = False

        bcc_container = MagicMock()
        bcc_container.count.return_value = 1

        bcc_hidden = MagicMock()
        bcc_hidden.count.return_value = 1
        bcc_hidden.input_value.side_effect = [
            existing_bcc_value, updated_bcc_value or existing_bcc_value,
        ]

        bcc_search = MagicMock()
        bcc_search.count.return_value = 1
        bcc_search.is_visible.return_value = True

        def email_form_locator(selector):
            if selector == "#email_options_subject":
                return subject
            if selector == "#send_button":
                return send
            if selector == "#s2id_email_options_bcc":
                return bcc_container
            if selector == "#email_options_bcc":
                return bcc_hidden
            if selector == "#s2id_email_options_bcc input.select2-input":
                return bcc_search
            return empty

        email_form.locator.side_effect = email_form_locator

        def page_locator(selector):
            if selector == "div.js-options-dropdown-toggle":
                return actions_collection
            if selector == 'a:has-text("Notify Tenant")':
                return notify_collection
            if selector == "#email_options_body":
                return body_field
            return empty

        page.locator.side_effect = page_locator
        return page, body_field, email_form, send, bcc_container, bcc_search, bcc_hidden

    def test_tenant_email_adds_manager_to_bcc_and_preserves_rachel(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            (
                page, body_field, email_form, send,
                bcc_container, bcc_search, bcc_hidden,
            ) = self._mock_notify_tenant_email_form(
                existing_bcc_value="rachel@clientco.example",
                updated_bcc_value=(
                    "rachel@clientco.example\t,\tmanager@clientco.example"
                ),
            )
            with patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "_validate_current_work_order_page", return_value=True
            ), patch.object(
                triage, "AppFolioReadAdapter"
            ):
                result = triage.send_tenant_email_via_appfolio(
                    page,
                    "Safe tenant email",
                    wo_url="https://example.appfolio.com/work_orders/9001",
                    wo_num="1700-1",
                    tenant="Jamie Tenant",
                    db_path=db_path,
                )
            self.assertTrue(result)
            bcc_container.click.assert_called_once_with()
            bcc_search.type.assert_called_once_with(
                triage.MANAGER_EMAIL, delay=30
            )
            page.keyboard.press.assert_any_call("Comma")
            body_field.fill.assert_called_once_with("Safe tenant email")
            send.click.assert_called_once_with()
            conn = sqlite3.connect(db_path)
            status, attempts = conn.execute(
                "SELECT status, attempt_count FROM appfolio_outbound_intents"
            ).fetchone()
            conn.close()
            self.assertEqual(status, "sent_verified")
            self.assertEqual(attempts, 1)

    def test_tenant_email_skips_bcc_edit_when_manager_already_present(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            (
                page, body_field, email_form, send,
                bcc_container, bcc_search, bcc_hidden,
            ) = self._mock_notify_tenant_email_form(
                existing_bcc_value=(
                    "rachel@clientco.example\t,\tmanager@clientco.example"
                ),
            )
            with patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "_validate_current_work_order_page", return_value=True
            ), patch.object(
                triage, "AppFolioReadAdapter"
            ):
                result = triage.send_tenant_email_via_appfolio(
                    page,
                    "Safe tenant email",
                    wo_url="https://example.appfolio.com/work_orders/9001",
                    wo_num="1700-1",
                    tenant="Jamie Tenant",
                    db_path=db_path,
                )
            self.assertTrue(result)
            bcc_container.click.assert_not_called()
            bcc_search.type.assert_not_called()
            send.click.assert_called_once_with()

    def test_exact_tenant_text_fills_and_clicks_once_then_completes(self):
        # Tenant texting is triggered from the tenant's own contact/People
        # record page (Aug 28 2026 finding), reached via a link on the
        # work-order page, then fills+sends the record page's own
        # composer (#messaging-input) - not an inline work-order-page
        # composer.
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            message = "One exact tenant message"
            page = MagicMock()
            page.url = (
                "https://example.appfolio.com/maintenance/"
                "service_requests/2000/work_orders/9001"
            )
            composer_collection = MagicMock()
            composer = MagicMock()
            container = MagicMock()
            send = MagicMock()
            body = MagicMock()
            tenant_link = MagicMock()
            send_trigger = MagicMock()

            composer_collection.count.return_value = 1
            composer_collection.nth.return_value = composer
            composer.is_visible.return_value = True
            composer.count.return_value = 1
            composer.input_value.side_effect = [message, ""]
            composer.locator.return_value = container
            container.count.return_value = 1
            container.get_by_role.return_value = send
            send.count.return_value = 1
            send.is_visible.return_value = True
            send.is_disabled.return_value = False
            body.inner_text.side_effect = [
                "Texting with Jamie Tenant",
                "Texting with Jamie Tenant",
                f"Texting with Jamie Tenant {message}",
            ]
            tenant_link.count.return_value = 1
            tenant_link.get_attribute.return_value = (
                "/occupancies/1/selected_tenant/2"
            )
            send_trigger.count.return_value = 1
            send_trigger.get_attribute.return_value = "Tenant"

            empty = MagicMock()
            empty.count.return_value = 0

            def locator(selector):
                if selector == "#messaging-input":
                    return composer_collection
                if selector == "body":
                    return body
                if selector == 'a.js-contact-card-name-link[href*="selected_tenant"]':
                    return tenant_link
                if selector == "a.js-send-text-message":
                    return send_trigger
                # A real Playwright locator for a non-matching CSS
                # selector finds zero elements, not an arbitrary visible
                # mock — model that explicitly instead of the default
                # MagicMock, which would fake a spurious "found" match
                # for every other candidate in a multi-selector tuple.
                return empty

            page.locator.side_effect = locator
            with patch.object(
                triage, "appfolio_write_allowed", return_value=(True, "test")
            ), patch.object(
                triage, "_is_trusted_work_order_url", return_value=True
            ), patch.object(
                triage, "_validate_current_work_order_page", return_value=True
            ), patch.object(
                triage, "AppFolioReadAdapter"
            ):
                result = triage.send_text_via_appfolio(
                    page,
                    page.url,
                    message,
                    tenant="Jamie Tenant",
                    wo_num="1700-1",
                    db_path=db_path,
                )
            self.assertTrue(result)
            composer.fill.assert_called_once_with(message)
            send.click.assert_called_once_with()
            conn = sqlite3.connect(db_path)
            status, attempts = conn.execute(
                "SELECT status, attempt_count FROM appfolio_outbound_intents"
            ).fetchone()
            conn.close()
            self.assertEqual(status, "sent_verified")
            self.assertEqual(attempts, 1)

    def test_schedule_test_mode_never_acquires_browser(self):
        with patch.object(vendor_lifecycle, "TEST_MODE", True), patch.object(
            vendor_lifecycle, "get_appfolio_browser_manager"
        ) as get_manager:
            result = vendor_lifecycle.update_wo_schedule(
                "https://example.appfolio.com/work_orders/9001",
                "08/15/2026",
                "10:00 AM",
                "12:00 PM",
            )

        self.assertFalse(result)
        get_manager.assert_not_called()

    def test_schedule_disabled_gate_never_acquires_browser(self):
        with patch.object(vendor_lifecycle, "TEST_MODE", False), patch.object(
            vendor_lifecycle,
            "appfolio_write_allowed",
            return_value=(False, "RACHEL_ENABLED=false"),
        ), patch.object(
            vendor_lifecycle, "get_appfolio_browser_manager"
        ) as get_manager:
            result = vendor_lifecycle.update_wo_schedule(
                "https://example.appfolio.com/work_orders/9001",
                "08/15/2026",
                "10:00 AM",
                "12:00 PM",
            )

        self.assertFalse(result)
        get_manager.assert_not_called()

    def test_exact_schedule_write_uses_claim_and_semantic_postcheck(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            wo_num = "2000-1"
            vendor_lifecycle.capture_tenant_availability(
                wo_num, "Monday morning", event_key="availability",
                db_path=db_path,
            )
            vendor_lifecycle.record_vendor_availability_delivery(
                wo_num, delivery_receipt="vendor-receipt",
                event_key="vendor-delivery", db_path=db_path,
            )
            window = vendor_lifecycle.record_vendor_two_hour_window(
                wo_num, date_value=FUTURE_DATE_SLASH,
                start_value="9:00 AM", end_value="11:00 AM",
                event_key="vendor-window", db_path=db_path,
            )
            ready = vendor_lifecycle.record_tenant_window_confirmation(
                wo_num,
                confirmed_window_fingerprint=window["window_fingerprint"],
                explicitly_confirmed=True,
                event_key="tenant-confirmed",
                db_path=db_path,
            )
            attempt_id = "exact-schedule-attempt"
            page = MagicMock()
            page.url = (
                "https://example.appfolio.com/maintenance/"
                "service_requests/2000/work_orders/9001"
            )
            actions = MagicMock()
            schedule_action = MagicMock()
            choose_different_time = MagicMock()
            date_field = MagicMock()
            time_field = MagicMock()
            duration = MagicMock()
            save = MagicMock()
            body = MagicMock()

            for element in (
                actions, schedule_action, choose_different_time,
                date_field, time_field, duration, save,
            ):
                element.is_visible.return_value = True
            date_field.input_value.return_value = FUTURE_DATE_SLASH
            save.count.return_value = 1
            save.is_visible.side_effect = [True, False]
            body.inner_text.side_effect = [
                "Work Order #2000-1",
                f"Scheduled {FUTURE_DATE_LONG} 9:00 AM to 11:00 AM",
            ]

            # AppFolio's Start Time / Duration fields are react-select
            # comboboxes (confirmed live Aug 28 2026): the input itself is
            # only ever queried for its containing control (to open the
            # dropdown) and its selected-value label (to verify the pick),
            # never filled or read via input_value().
            time_container = MagicMock()
            time_container.count.return_value = 1
            time_selected_value = MagicMock()
            time_selected_value.inner_text.return_value = "9:00 AM"

            def time_field_locator(selector):
                if selector == vendor_lifecycle.combined_selector(
                    "work_order.schedule_control_container"
                ):
                    return time_container
                if selector == vendor_lifecycle.combined_selector(
                    "work_order.schedule_selected_value"
                ):
                    return time_selected_value
                raise AssertionError(f"unexpected time_field selector {selector}")

            time_field.locator.side_effect = time_field_locator

            duration_container = MagicMock()
            duration_container.count.return_value = 1
            duration_selected_value = MagicMock()
            duration_selected_value.inner_text.return_value = "2 Hours"

            def duration_locator(selector):
                if selector == vendor_lifecycle.combined_selector(
                    "work_order.schedule_control_container"
                ):
                    return duration_container
                if selector == vendor_lifecycle.combined_selector(
                    "work_order.schedule_selected_value"
                ):
                    return duration_selected_value
                raise AssertionError(f"unexpected duration selector {selector}")

            duration.locator.side_effect = duration_locator

            time_option = MagicMock()
            time_option.count.return_value = 1
            time_option.is_visible.return_value = True
            duration_option = MagicMock()
            duration_option.count.return_value = 1
            duration_option.is_visible.return_value = True
            option_root = MagicMock()
            option_root.filter.side_effect = [time_option, duration_option]

            mapping = {
                "div.js-options-dropdown-toggle": actions,
                'a:has-text("Schedule")': schedule_action,
                'label:has-text("Choose different time")': (
                    choose_different_time
                ),
                "#start-date-input": date_field,
                "#start-time-input": time_field,
                "#duration-input": duration,
                "button.js-schedule-save": save,
            }

            def collection(element):
                result = MagicMock()
                result.count.return_value = 1
                result.nth.return_value = element
                return result

            def locator(selector):
                if selector == "body":
                    return body
                if selector == vendor_lifecycle.combined_selector("work_order.schedule_option"):
                    return option_root
                if selector in mapping:
                    return collection(mapping[selector])
                result = MagicMock()
                result.count.return_value = 0
                return result

            page.locator.side_effect = locator
            manager = MagicMock()
            manager.operation.return_value.__enter__.return_value = page
            with patch.object(
                vendor_lifecycle, "DB_PATH", db_path
            ), patch.object(
                vendor_lifecycle, "TEST_MODE", False
            ), patch.object(
                vendor_lifecycle, "appfolio_write_allowed",
                return_value=(True, "test"),
            ), patch.object(
                vendor_lifecycle, "get_appfolio_browser_manager",
                return_value=manager,
            ), patch.object(
                vendor_lifecycle, "human_delay"
            ):
                receipt = vendor_lifecycle.update_wo_schedule(
                    page.url,
                    FUTURE_DATE_SLASH,
                    "9:00 AM",
                    "11:00 AM",
                    wo_num=wo_num,
                    tenant="Jamie Tenant",
                    vendor_name="Safe Vendor",
                    write_attempt_id=attempt_id,
                )
            self.assertTrue(str(receipt).startswith(
                "schedule_window_semantically_verified_"
            ))
            date_field.fill.assert_called_once_with(FUTURE_DATE_SLASH)
            date_field.press.assert_called_once_with("Escape")
            time_container.click.assert_called_once_with()
            duration_container.click.assert_called_once_with()
            time_option.click.assert_called_once_with()
            duration_option.click.assert_called_once_with()
            save.click.assert_called_once_with()

    def test_schedule_dom_preflight_failure_does_not_consume_write_claim(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            wo_num = "2001-1"
            vendor_lifecycle.capture_tenant_availability(
                wo_num, "Monday morning", event_key="availability",
                db_path=db_path,
            )
            vendor_lifecycle.record_vendor_availability_delivery(
                wo_num, delivery_receipt="vendor-receipt",
                event_key="vendor-delivery", db_path=db_path,
            )
            window = vendor_lifecycle.record_vendor_two_hour_window(
                wo_num, date_value=FUTURE_DATE_SLASH,
                start_value="9:00 AM", end_value="11:00 AM",
                event_key="vendor-window", db_path=db_path,
            )
            vendor_lifecycle.record_tenant_window_confirmation(
                wo_num,
                confirmed_window_fingerprint=window["window_fingerprint"],
                explicitly_confirmed=True,
                event_key="tenant-confirmed",
                db_path=db_path,
            )
            page = MagicMock()
            page.url = "https://example.appfolio.com/work_orders/9002"
            body = MagicMock()
            body.inner_text.return_value = "Work Order 2001-1"
            empty = MagicMock()
            empty.count.return_value = 0

            def locator(selector):
                return body if selector == "body" else empty

            page.locator.side_effect = locator
            manager = MagicMock()
            manager.operation.return_value.__enter__.return_value = page
            with patch.object(
                vendor_lifecycle, "DB_PATH", db_path
            ), patch.object(
                vendor_lifecycle, "TEST_MODE", False
            ), patch.object(
                vendor_lifecycle, "appfolio_write_allowed",
                return_value=(True, "test"),
            ), patch.object(
                vendor_lifecycle, "get_appfolio_browser_manager",
                return_value=manager,
            ), patch.object(vendor_lifecycle, "human_delay"):
                result = vendor_lifecycle.update_wo_schedule(
                    page.url,
                    FUTURE_DATE_SLASH,
                    "9:00 AM",
                    "11:00 AM",
                    wo_num=wo_num,
                    tenant="Jamie Tenant",
                    vendor_name="Safe Vendor",
                    write_attempt_id="preflight-failure-attempt",
                )

            self.assertFalse(result)
            self.assertEqual(
                vendor_lifecycle.get_schedule(
                    wo_num, db_path=db_path
                )["state"],
                vendor_lifecycle.READY_TO_SCHEDULE,
            )
            conn = sqlite3.connect(db_path)
            claim_count = conn.execute(
                "SELECT COUNT(*) FROM maintenance_schedule_events "
                "WHERE event_kind = 'appfolio_schedule_write_claimed'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(claim_count, 0)


if __name__ == "__main__":
    unittest.main()
