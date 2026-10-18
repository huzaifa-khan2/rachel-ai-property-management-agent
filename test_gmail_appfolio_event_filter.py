import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_inbox_monitor as monitor


class GmailAppFolioEventFilterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = monitor.DB_PATH
        monitor.DB_PATH = os.path.join(self.tempdir.name, "rachel.db")

    def tearDown(self):
        monitor.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_accepted_notification_never_acquires_browser(self):
        email_data = {
            "subject": "Work Order #1634-2 has been accepted",
            "body": "Work Order #1634-2 has been accepted by a vendor.",
        }
        with patch.object(
            monitor, "get_appfolio_browser_manager"
        ) as get_manager, patch.object(
            monitor, "triage_work_order_details"
        ) as triage_details:
            monitor.handle_appfolio_work_order(email_data)
        get_manager.assert_not_called()
        triage_details.assert_not_called()

    def test_original_new_work_order_keeps_targeted_browser_path(self):
        email_data = {
            "subject": "WO #1700-1 - New maintenance request",
            "body": (
                "Work Order: 1700-1\n"
                "Resident Name: Jamie Tenant\n"
                "Address: 100 Oak Street Unit 2\n"
                "Job Description\nLeaking kitchen faucet\n\n"
                "Issue Descriptor: Plumbing\n"
                "https://clientco.appfolio.com/"
                "maintenance/service_requests/17/work_orders/1700"
            ),
        }
        manager = MagicMock()
        page = MagicMock()
        manager.operation.return_value.__enter__.return_value = page
        with patch.object(
            monitor, "get_appfolio_browser_manager", return_value=manager
        ) as get_manager, patch.object(
            monitor, "triage_work_order_details"
        ) as triage_details:
            monitor.handle_appfolio_work_order(email_data)
        get_manager.assert_called_once_with()
        manager.operation.assert_called_once_with("email_new_work_order")
        triage_details.assert_called_once()
        self.assertIs(triage_details.call_args.kwargs["page"], page)

    def test_internal_new_work_order_does_not_acquire_tenant_triage_browser(self):
        email_data = {
            "subject": "WO #1701-1 - Internal maintenance request",
            "body": (
                "Work Order: 1701-1\n"
                "Request Type: Internal\n"
                "Address: 100 Oak Street Unit 2\n"
                "Job Description\nRoutine inspection\n\n"
                "Issue Descriptor: General\n"
                "https://clientco.appfolio.com/"
                "maintenance/service_requests/17/work_orders/1701"
            ),
        }
        with patch.object(
            monitor, "get_appfolio_browser_manager"
        ) as get_manager, patch.object(
            monitor, "triage_work_order_details"
        ) as triage_details:
            monitor.handle_appfolio_work_order(email_data)
        get_manager.assert_not_called()
        triage_details.assert_not_called()


if __name__ == "__main__":
    unittest.main()
