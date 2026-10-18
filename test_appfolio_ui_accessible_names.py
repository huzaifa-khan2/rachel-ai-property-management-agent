"""
Offline regression coverage for the centralized AppFolio accessible-name
registry (rachel_appfolio_ui.APPFOLIO_ACCESSIBLE_NAMES / accessible_name)
and its use in rachel_wo_triage.py's work-order write-adjacent functions.

Covers the AppFolio selector-hardening migration: get_by_role/get_by_label/
get_by_text literals ("+ Add", "Job Description", "Save",
"Resident Availability") that used to be scattered inline are now looked
up from one place. These tests lock the exact literal values (a rename
here must be deliberate) and prove real call sites resolve to them end to
end, not just that the registry itself works in isolation.

"Edit" and "Email" were removed from this registry Aug 28 2026: live
testing showed both underlying controls are <a> elements with no href,
so they are matched by CSS class/href-absence (work_order.edit_control's
successor is work_order.dispatch.edit_candidates;
work_order.email_link_control's successor is
work_order.vendor_email_action) - accessible-name/role lookups could
never find either of them.

No Playwright, no live AppFolio - MagicMock pages only.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_appfolio_ui as ui
import rachel_wo_triage as triage


class AccessibleNameRegistryTests(unittest.TestCase):
    def test_known_names_resolve_to_exact_locked_literals(self):
        self.assertEqual(
            ui.accessible_name("work_order.add_control"), "+ Add"
        )
        self.assertEqual(
            ui.accessible_name("work_order.job_description_label"),
            "Job Description",
        )
        self.assertEqual(
            ui.accessible_name("work_order.save_button"), "Save"
        )
        self.assertEqual(
            ui.accessible_name("work_order.resident_availability_marker"),
            "Resident Availability",
        )
        self.assertEqual(
            ui.accessible_name("work_order.send_button"), "Send"
        )

    def test_unknown_name_fails_closed(self):
        with self.assertRaises(ui.AppFolioUIContractError):
            ui.accessible_name("work_order.nonexistent")

    def test_registry_has_no_duplicate_values_hiding_a_real_merge_target(self):
        # Every accessible name should map to a distinct label/control -
        # if two keys ever collide on value, that's exactly the kind of
        # genuine duplicate the centralization is supposed to catch.
        values = list(ui.APPFOLIO_ACCESSIBLE_NAMES.values())
        self.assertEqual(len(values), len(set(values)))


class AccessibleNameRegistryValidationTests(unittest.TestCase):
    def test_current_registry_validates_clean(self):
        self.assertEqual(ui.validate_accessible_name_registry(), [])

    def test_validation_is_offline_only(self):
        # Structural proof, not just an assertion: the validator must not
        # import or reference anything network/browser-capable.
        import inspect

        source = inspect.getsource(ui.validate_accessible_name_registry)
        source += inspect.getsource(ui._accessible_names_source_duplicate_keys)
        for forbidden in ("playwright", "requests", "socket", "urlopen"):
            self.assertNotIn(forbidden, source.lower())

    def test_required_names_are_all_present(self):
        for required in ui.REQUIRED_ACCESSIBLE_NAMES:
            self.assertIn(required, ui.APPFOLIO_ACCESSIBLE_NAMES)

    def test_missing_required_name_is_detected(self):
        original = dict(ui.APPFOLIO_ACCESSIBLE_NAMES)
        try:
            del ui.APPFOLIO_ACCESSIBLE_NAMES["work_order.save_button"]
            errors = ui.validate_accessible_name_registry()
        finally:
            ui.APPFOLIO_ACCESSIBLE_NAMES.clear()
            ui.APPFOLIO_ACCESSIBLE_NAMES.update(original)
        self.assertIn(
            "accessible_name_missing:work_order.save_button", errors
        )

    def test_empty_value_is_detected(self):
        original = dict(ui.APPFOLIO_ACCESSIBLE_NAMES)
        try:
            ui.APPFOLIO_ACCESSIBLE_NAMES["work_order.save_button"] = "   "
            errors = ui.validate_accessible_name_registry()
        finally:
            ui.APPFOLIO_ACCESSIBLE_NAMES.clear()
            ui.APPFOLIO_ACCESSIBLE_NAMES.update(original)
        self.assertIn(
            "accessible_name_value_empty:work_order.save_button", errors
        )

    def test_undocumented_duplicate_value_is_detected(self):
        original = dict(ui.APPFOLIO_ACCESSIBLE_NAMES)
        try:
            ui.APPFOLIO_ACCESSIBLE_NAMES["work_order.duplicate_probe"] = (
                ui.APPFOLIO_ACCESSIBLE_NAMES["work_order.save_button"]
            )
            errors = ui.validate_accessible_name_registry()
        finally:
            ui.APPFOLIO_ACCESSIBLE_NAMES.clear()
            ui.APPFOLIO_ACCESSIBLE_NAMES.update(original)
        self.assertTrue(
            any(
                error.startswith("accessible_name_value_duplicated:Save:")
                for error in errors
            )
        )

    def test_source_has_no_duplicate_dict_keys(self):
        self.assertEqual(ui._accessible_names_source_duplicate_keys(), [])

    def test_foundation_health_reports_accessible_name_validation(self):
        import rachel_foundation_health as health

        report = health.build_foundation_health_report()
        self.assertEqual(report["matrix"]["status"], "healthy")


class DeadSelectorRemovalTests(unittest.TestCase):
    def test_dead_email_link_css_selector_was_removed(self):
        self.assertNotIn("work_order.email_link", ui.APPFOLIO_SELECTORS)

    def test_active_precise_email_action_selector_remains_authoritative(self):
        # Superseded (Aug 28 2026): the vendor Email action has no href,
        # so it can never be exposed via accessibility role "link" - it
        # is matched by CSS class instead, not an accessible name.
        self.assertEqual(
            ui.combined_selector("work_order.vendor_email_action"),
            "a.js-work-order-email",
        )

    def test_removing_dead_entry_did_not_break_contract_registry(self):
        self.assertEqual(ui.validate_contract_registry(), [])


class WorkOrderIssueOptionHelperTests(unittest.TestCase):
    def test_supplies_only_business_value_to_centralized_helper(self):
        page = MagicMock()
        result = ui.work_order_issue_option(page, "  Leaking faucet  ")
        page.get_by_role.assert_called_once_with(
            "option", name="Leaking faucet", exact=True
        )
        self.assertIs(result, page.get_by_role.return_value)

    def test_create_additional_work_order_uses_the_centralized_helper(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            triage, "appfolio_write_allowed", return_value=(True, "test")
        ), patch.object(
            triage, "unique_visible", return_value=MagicMock()
        ), patch.object(
            triage, "work_order_issue_option"
        ) as mock_helper:
            page = MagicMock()
            page.url = (
                "https://clientco.appfolio.com/"
                "maintenance/service_requests/30"
            )
            add = MagicMock()
            add.count.return_value = 1
            add.is_visible.return_value = True
            page.get_by_text.return_value = add
            description = MagicMock()
            description.count.return_value = 1
            description.is_visible.return_value = True
            page.get_by_label.return_value = description
            form = MagicMock()
            form.count.return_value = 1
            description.locator.return_value = form
            save = MagicMock()
            save.count.return_value = 1
            save.is_visible.return_value = True
            save.is_disabled.return_value = False
            form.get_by_role.return_value = save
            mock_helper.return_value.count.return_value = 0  # fail closed

            db_path = os.path.join(root, "rachel.db")
            triage.create_additional_work_order(
                page, "Second issue", "Repair second issue",
                db_path=db_path,
            )
        mock_helper.assert_called_once_with(page, "Second issue")


class SendButtonAccessibleNameTests(unittest.TestCase):
    def test_send_text_via_appfolio_uses_the_centralized_literal(self):
        # Tenant texting is triggered from the tenant's own contact/People
        # record page (Aug 28 2026 finding), reached via a link on the
        # work-order page - not an inline work-order-page composer.
        with tempfile.TemporaryDirectory() as root, patch.object(
            triage, "appfolio_write_allowed", return_value=(True, "test")
        ), patch.object(
            triage, "_is_trusted_work_order_url", return_value=True
        ), patch.object(
            triage, "_validate_current_work_order_page", return_value=True
        ), patch.object(
            triage, "unique_visible", return_value=MagicMock()
        ), patch.object(
            triage, "AppFolioReadAdapter"
        ):
            page = MagicMock()
            tenant_link = MagicMock()
            tenant_link.count.return_value = 1
            tenant_link.get_attribute.return_value = (
                "/occupancies/1/selected_tenant/2"
            )
            send_trigger = MagicMock()
            send_trigger.count.return_value = 1
            send_trigger.get_attribute.return_value = "Tenant"
            body_locator = MagicMock()
            body_locator.inner_text.return_value = "Texting with Jamie Tenant"

            def locator_side_effect(selector):
                if selector == 'a.js-contact-card-name-link[href*="selected_tenant"]':
                    return tenant_link
                if selector == "a.js-send-text-message":
                    return send_trigger
                if selector == "body":
                    return body_locator
                return MagicMock()

            page.locator.side_effect = locator_side_effect

            composer = triage.unique_visible.return_value
            container = MagicMock()
            container.count.return_value = 1
            composer.locator.return_value = container
            send = MagicMock()
            send.count.return_value = 0  # fail closed right after lookup
            container.get_by_role.return_value = send

            db_path = os.path.join(root, "rachel.db")
            triage.send_text_via_appfolio(
                page,
                "https://clientco.appfolio.com/"
                "maintenance/service_requests/30/work_orders/3001",
                "Test message", True,
                tenant="jamie tenant", wo_num="3001-1", db_path=db_path,
            )
        container.get_by_role.assert_called_once_with(
            "button", name="Send", exact=True
        )


class CreateAdditionalWorkOrderAccessibleNameTests(unittest.TestCase):
    def _blocked_after_add_click(self):
        """A page mock where "+ Add" resolves and is clicked, then the
        function fails closed on the next lookup - enough to observe the
        get_by_text call_args without simulating the full write flow."""
        page = MagicMock()
        page.url = (
            "https://clientco.appfolio.com/"
            "maintenance/service_requests/30"
        )
        add = MagicMock()
        add.count.return_value = 1
        add.is_visible.return_value = True
        page.get_by_text.return_value = add
        # Force an early, safe exit right after "+ Add" is used: no issue
        # search field found.
        return page

    def test_add_control_lookup_uses_the_centralized_literal(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            triage, "appfolio_write_allowed", return_value=(True, "test")
        ), patch.object(
            triage, "unique_visible", return_value=None
        ):
            page = self._blocked_after_add_click()
            db_path = os.path.join(root, "rachel.db")
            result = triage.create_additional_work_order(
                page, "Second issue", "Repair second issue",
                db_path=db_path,
            )
        self.assertFalse(result)
        page.get_by_text.assert_called_once_with("+ Add", exact=True)

    def test_job_description_and_save_lookups_use_centralized_literals(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            triage, "appfolio_write_allowed", return_value=(True, "test")
        ), patch.object(
            triage, "unique_visible", return_value=MagicMock()
        ):
            page = self._blocked_after_add_click()
            description = MagicMock()
            description.count.return_value = 1
            description.is_visible.return_value = True
            page.get_by_label.return_value = description
            form = MagicMock()
            form.count.return_value = 1
            description.locator.return_value = form
            save = MagicMock()
            save.count.return_value = 0  # fail closed right after lookup
            form.get_by_role.return_value = save

            db_path = os.path.join(root, "rachel.db")
            result = triage.create_additional_work_order(
                page, "Second issue", "Repair second issue",
                db_path=db_path,
            )
        self.assertFalse(result)
        page.get_by_label.assert_called_once_with(
            "Job Description", exact=True
        )
        form.get_by_role.assert_called_once_with(
            "button", name="Save", exact=True
        )


class EditWoDescriptionAccessibleNameTests(unittest.TestCase):
    def test_edit_job_description_and_save_lookups_use_centralized_literals(
        self,
    ):
        with tempfile.TemporaryDirectory() as root, patch.object(
            triage, "appfolio_write_allowed", return_value=(True, "test")
        ), patch.object(
            triage, "_is_trusted_work_order_url", return_value=True
        ), patch.object(
            triage, "_validate_current_work_order_page", return_value=True
        ), patch.object(
            triage, "AppFolioReadAdapter"
        ):
            page = MagicMock()
            page.url = (
                "https://clientco.appfolio.com/"
                "maintenance/service_requests/30/work_orders/3001"
            )
            edit_candidates = MagicMock()
            edit = MagicMock()
            edit.count.return_value = 1
            edit.is_visible.return_value = True
            edit_candidates.filter.return_value = edit
            description = MagicMock()
            description.count.return_value = 0  # fail closed right after

            def locator(selector):
                if selector == ui.combined_selector(
                    "work_order.dispatch.edit_candidates"
                ):
                    return edit_candidates
                if selector == ui.combined_selector(
                    "work_order.job_description_field"
                ):
                    return description
                raise AssertionError(f"unexpected selector {selector}")

            page.locator.side_effect = locator

            db_path = os.path.join(root, "rachel.db")
            result = triage.edit_wo_description(
                page,
                "https://clientco.appfolio.com/"
                "maintenance/service_requests/30/work_orders/3001",
                "Updated description",
                wo_num="3001-1",
                db_path=db_path,
            )
        self.assertFalse(result)
        edit_candidates.filter.assert_called_once()
        filter_pattern = edit_candidates.filter.call_args.kwargs["has_text"]
        self.assertEqual(filter_pattern.pattern, r"^\s*Edit\s*$")


if __name__ == "__main__":
    unittest.main()
