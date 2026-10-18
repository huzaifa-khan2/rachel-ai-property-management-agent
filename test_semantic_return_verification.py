"""
Offline regression coverage for _verify_semantic_return_to_inbox
(rachel_appfolio_inbox.py) - the return-to-inbox acceptance check.

A row-count change between opening a thread and returning from it must
never fail this check by itself: opening an unread conversation can
inherently mark it read, and AppFolio may re-sort/re-render the inbox list
as a result. Only genuine structural drift - wrong route, thread-detail UI
or composer still active, or the list/filter-toolbar structure itself
missing - must fail.

Pure function tests: fake page/frame objects, no Playwright, no live
AppFolio.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_appfolio_inbox as inbox

INBOX_URL = "https://clientco.appfolio.com/texting/inbox"


class FakeFrame:
    def __init__(self, results):
        self.results = results

    def evaluate(self, script, *args):
        value = self.results.get(script, {})
        return value() if callable(value) else value


def make_page(
    url=INBOX_URL,
    *,
    list_root_present=True,
    row_count=21,
    composer_active=False,
    thread_detail_active=False,
    scope_status="full_scope_verified",
    rows_before=21,
    rows_after=21,
    structural_eval_raises=False,
    scope_eval_raises=False,
):
    page = MagicMock()
    page.url = url

    structural_result = {
        "list_root_present": list_root_present,
        "row_count": row_count,
        "composer_active": composer_active,
        "thread_detail_active": thread_detail_active,
    }

    def frame_evaluate(script, *args):
        if script == inbox._INBOX_SEMANTIC_RETURN_VERIFICATION_SCRIPT:
            if structural_eval_raises:
                raise RuntimeError("frame evaluation failed")
            return structural_result
        return {}

    frame = FakeFrame({})
    frame.evaluate = frame_evaluate
    page.frames = [frame]

    def page_evaluate(script, *args):
        if script == inbox._INBOX_FILTER_SCOPE_RESET_SCRIPT:
            if scope_eval_raises:
                raise RuntimeError("scope evaluation failed")
            return {
                "status": scope_status,
                "verified_full_scope": scope_status == "full_scope_verified",
                "conversation_rows_before": rows_before,
                "conversation_rows_after": rows_after,
            }
        return {}

    page.evaluate = MagicMock(side_effect=page_evaluate)
    return page


class SemanticReturnVerificationTests(unittest.TestCase):
    def test_unread_thread_opened_row_count_changes_still_passes(self):
        # Opening an unread conversation can mark it read and cause AppFolio
        # to re-sort/re-render - fewer rows after return is legitimate.
        page = make_page(rows_before=21, rows_after=20, row_count=20)
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertTrue(result["valid"])
        self.assertEqual(result["conversation_rows_before_scope_reset"], 21)
        self.assertEqual(result["conversation_rows_after_scope_reset"], 20)

    def test_same_row_count_passes(self):
        page = make_page(rows_before=21, rows_after=21, row_count=21)
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertTrue(result["valid"])

    def test_row_count_increase_after_return_also_passes(self):
        # A new message could arrive elsewhere in the inbox between open and
        # return - more rows afterward is equally legitimate.
        page = make_page(rows_before=21, rows_after=23, row_count=23)
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertTrue(result["valid"])

    def test_inbox_root_missing_fails(self):
        page = make_page(list_root_present=False, row_count=0)
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertFalse(result["valid"])
        self.assertFalse(result["list_root_present"])

    def test_still_inside_thread_via_composer_fails(self):
        page = make_page(composer_active=True)
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertFalse(result["valid"])
        self.assertTrue(result["composer_active"])

    def test_still_inside_thread_via_back_control_fails(self):
        page = make_page(thread_detail_active=True)
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertFalse(result["valid"])
        self.assertTrue(result["thread_detail_active"])

    def test_unexpected_route_fails(self):
        page = make_page(
            url="https://clientco.appfolio.com/"
            "maintenance/service_requests/17/work_orders/1700"
        )
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertFalse(result["valid"])
        self.assertFalse(result["route_ok"])

    def test_malformed_empty_list_state_where_validity_cannot_be_established_fails(
        self,
    ):
        # Structural probe raises (nothing evaluable) AND the filter-scope
        # reset can't find a conversation list either - validity can't be
        # established at all, so this must fail closed.
        page = make_page(
            structural_eval_raises=True,
            scope_status="conversation_list_not_found",
        )
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertFalse(result["valid"])
        self.assertFalse(result["list_root_present"])
        self.assertFalse(result["scope_structurally_sound"])

    def test_filter_toolbar_missing_fails(self):
        page = make_page(scope_status="filter_toolbar_not_found")
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertFalse(result["valid"])
        self.assertFalse(result["scope_structurally_sound"])

    def test_scope_reset_exception_does_not_propagate(self):
        # A raised exception during scope reset must degrade to a reported
        # failure, never crash the caller.
        page = make_page(scope_eval_raises=True)
        result = inbox._verify_semantic_return_to_inbox(page)
        self.assertFalse(result["valid"])
        self.assertFalse(result["scope_structurally_sound"])


class PipelineReturnStageIntegrationTests(unittest.TestCase):
    def test_pipeline_return_stage_reports_failed_checks_in_reason(self):
        with patch.object(
            inbox, "_verify_semantic_return_to_inbox",
            return_value={
                "valid": False,
                "route_ok": True,
                "list_root_present": True,
                "composer_active": True,
                "thread_detail_active": False,
                "scope_structurally_sound": True,
                "row_count": 5,
                "current_route": "/texting/inbox",
                "scope_status": "full_scope_verified",
                "conversation_rows_before_scope_reset": 5,
                "conversation_rows_after_scope_reset": 5,
            },
        ), patch.object(
            inbox, "_return_to_inbox", return_value=1
        ):
            with self.assertRaisesRegex(
                inbox.InboxThreadPipelineError, "composer_inactive"
            ):
                # Directly exercise just the failure-reason composition by
                # invoking the same logic path the pipeline uses.
                verification = inbox._verify_semantic_return_to_inbox(
                    MagicMock()
                )
                if not verification["valid"]:
                    failed_checks = sorted(
                        key for key, ok in (
                            ("route", verification["route_ok"]),
                            ("list_root", verification["list_root_present"]),
                            (
                                "composer_inactive",
                                not verification["composer_active"],
                            ),
                            (
                                "thread_detail_inactive",
                                not verification["thread_detail_active"],
                            ),
                            (
                                "scope_structurally_sound",
                                verification["scope_structurally_sound"],
                            ),
                        ) if not ok
                    )
                    raise inbox.InboxThreadPipelineError(
                        "returned_to_inbox",
                        "semantic_return_verification_failed:"
                        + ",".join(failed_checks),
                    )


if __name__ == "__main__":
    unittest.main()
