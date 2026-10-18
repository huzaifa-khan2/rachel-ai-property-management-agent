import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from rachel_appfolio_browser import AppFolioBrowserManager
from rachel_appfolio_ui import (
    APPFOLIO_DOM_CONTRACTS,
    APPFOLIO_SELECTORS,
    AppFolioFeatureHealth,
    AppFolioReadAdapter,
    AppFolioUIContractError,
    FeatureCircuitOpen,
    WorkOrderDetailPage,
    operation_feature,
    route_matches,
    sanitized_failure_reason,
    validate_contract_registry,
)
from rachel_event_memory import initialize_event_memory
from rachel_foundation_health import (
    FOUNDATION_SUBSYSTEMS,
    audit_active_appfolio_architecture,
    assert_zero_write_metrics,
    build_foundation_health_report,
    execute_validation_matrix,
    load_contract_fixtures,
    validate_contract_fixtures,
)


ROOT = pathlib.Path(__file__).resolve().parent


class FakeReadPage:
    def __init__(self, url="https://example.test/texting/inbox"):
        self.url = url
        self.goto_calls = 0
        self.waits = []

    def goto(self, url, **_kwargs):
        self.goto_calls += 1
        self.url = url

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class FakeBrowserPage:
    def __init__(self):
        self.url = "https://example.test/dashboard"
        self.main_frame = self
        self.events = {}

    def evaluate(self, _script):
        return True

    def on(self, name, callback):
        self.events[name] = callback


class FakeBrowserContext:
    def __init__(self):
        self.pages = [FakeBrowserPage()]

    def close(self):
        return None


class FakePlaywright:
    def __init__(self):
        self.chromium = self

    def start(self):
        return self

    def launch_persistent_context(self, **_kwargs):
        return FakeBrowserContext()

    def stop(self):
        return None


class FoundationContractMatrixTests(unittest.TestCase):
    def test_contract_registry_is_complete_and_valid(self):
        self.assertEqual(validate_contract_registry(), [])
        for contract in APPFOLIO_DOM_CONTRACTS.values():
            self.assertGreaterEqual(len(contract.identity_invariants), 2)
            self.assertTrue(contract.route_patterns)
            self.assertTrue(all(
                name in APPFOLIO_SELECTORS
                for name in contract.selector_names
            ))

    def test_sanitized_fixture_matrix_executes_all_78_cases(self):
        fixtures = load_contract_fixtures()
        result = execute_validation_matrix(fixtures)
        self.assertEqual(result["cases_executed"], 78)
        self.assertEqual(result["cases_passed"], 78)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["status"], "healthy")

    def test_fixture_set_contains_no_email_phone_or_url_values(self):
        fixtures = load_contract_fixtures()
        self.assertEqual(validate_contract_fixtures(fixtures), [])
        serialized = json.dumps(fixtures)
        self.assertNotRegex(serialized, r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)

    def test_first_city_is_not_special_cased_in_production(self):
        production = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in (
                "rachel_appfolio_ui.py", "rachel_appfolio_inbox.py",
                "rachel_communication_workflow.py", "rachel_safety.py",
            )
        )
        self.assertNotIn("First City Construction", production)
        self.assertNotIn(
            "9ea0d5b924f44c7af05bc606e79deb73186e8ddd67ed746fde29d9dd92185d82",
            production,
        )

    def test_live_thread_contract_evidence_is_sanitized_and_representative(self):
        fixture = load_contract_fixtures()["communication_thread.json"]
        evidence = fixture["live_contract_evidence"]
        self.assertEqual(evidence["semantic_thread_frame_count"], 1)
        self.assertEqual(evidence["mounted_message_count"], 50)
        self.assertEqual(evidence["composer_textarea_count"], 1)
        self.assertEqual(evidence["send_control_count"], 1)
        self.assertEqual(evidence["back_control_count"], 1)
        self.assertEqual(evidence["virtualization_hint_count"], 0)
        self.assertEqual(evidence["lazy_load_hint_count"], 0)

    def test_legacy_thread_predicate_failure_is_captured(self):
        evidence = load_contract_fixtures()[
            "communication_thread.json"
        ]["live_contract_evidence"]
        self.assertEqual(evidence["legacy_history_candidate_count"], 0)
        self.assertEqual(evidence["legacy_verified_frame_count"], 0)

    def test_foundation_report_names_every_requested_subsystem(self):
        report = build_foundation_health_report()
        self.assertEqual(
            {item["subsystem"] for item in report["subsystems"]},
            set(FOUNDATION_SUBSYSTEMS),
        )
        self.assertEqual(report["summary"], {
            "healthy": 10, "degraded": 0, "disabled": 0,
        })
        self.assertFalse(report["live_activity_performed"])

    def test_active_appfolio_architecture_audit_is_healthy(self):
        audit = audit_active_appfolio_architecture()
        self.assertEqual(audit["status"], "healthy")
        self.assertEqual(audit["errors"], [])
        self.assertEqual(audit["persistent_context_owners"], 1)
        self.assertEqual(audit["login_implementations"], 1)
        self.assertGreaterEqual(audit["dom_contracts"], 7)
        self.assertGreaterEqual(audit["feature_circuits"], 10)
        self.assertGreaterEqual(audit["fail_closed_legacy_paths"], 1)

    def test_every_health_entry_has_exact_reason(self):
        for item in build_foundation_health_report()["subsystems"]:
            self.assertIn(item["status"], {"healthy", "degraded", "disabled"})
            self.assertTrue(item["reason"])
            self.assertGreater(item["contract_cases"], 0)

    def test_read_only_fixture_states_never_expect_workflow_actions(self):
        fixtures = load_contract_fixtures()
        for fixture in fixtures.values():
            for case in fixture["cases"]:
                if "workflow_actions_attempted" in case:
                    self.assertEqual(case["workflow_actions_attempted"], 0)
                for key in ("expected_writes", "typing", "sending"):
                    if key in case:
                        self.assertEqual(case[key], 0)

    def test_reconciliation_matrix_has_no_detail_cap(self):
        source = (ROOT / "rachel_work_order_scanner.py").read_text()
        body = source[
            source.index("def reconcile_logged_in_page("):
            source.index("def scan_logged_in_page(")
        ]
        self.assertNotIn("max_detail", body.lower())
        self.assertNotIn("detail_cap", body.lower())
        self.assertNotIn("candidates[:", body)


class UIAdapterTests(unittest.TestCase):
    def test_route_contracts_accept_dynamic_segments(self):
        self.assertTrue(route_matches(
            "https://example.test/texting/inbox/thread-token",
            "/texting/inbox/:conversation",
        ))
        self.assertTrue(route_matches(
            "https://example.test/maintenance/service_requests/12/work_orders/34",
            "/maintenance/service_requests/:request/work_orders/:work_order",
        ))
        self.assertFalse(route_matches(
            "https://example.test/properties/34",
            "/maintenance/service_requests/:request/work_orders/:work_order",
        ))

    def test_bounded_read_retries_transient_node_replacement(self):
        page = FakeReadPage()
        attempts = []

        def reader():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("react node replaced")
            return {"semantic_identity": "same"}

        result = AppFolioReadAdapter(
            page, "communication_thread_reader"
        ).read("semantic_postcheck", reader, attempts=3, delay_seconds=0)
        self.assertEqual(result, {"semantic_identity": "same"})
        self.assertEqual(len(attempts), 3)

    def test_bounded_read_raises_after_configured_attempts(self):
        page = FakeReadPage()
        with self.assertRaisesRegex(RuntimeError, "still unavailable"):
            AppFolioReadAdapter(page, "communication_inbox").read(
                "row_mount",
                lambda: (_ for _ in ()).throw(
                    RuntimeError("still unavailable")
                ),
                attempts=2,
                delay_seconds=0,
            )

    def test_navigate_read_retries_and_verifies_route(self):
        page = FakeReadPage()
        adapter = AppFolioReadAdapter(page, "communication_inbox")
        result = adapter.navigate_read(
            "https://example.test/texting/inbox",
            route_pattern="/texting/inbox",
        )
        self.assertEqual(result, "https://example.test/texting/inbox")
        self.assertEqual(page.goto_calls, 1)

    def test_work_order_identity_requires_exact_visible_number(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = (
            "Work Order #1700-1\nStatus NEW"
        )
        detail = WorkOrderDetailPage(page)
        self.assertTrue(detail.has_semantic_identity("1700-1"))
        self.assertFalse(detail.has_semantic_identity("1700-2"))

    def test_exception_diagnostic_does_not_include_raw_error_text(self):
        reason = sanitized_failure_reason(
            RuntimeError("resident name, phone, and message text")
        )
        self.assertEqual(reason, "exception_type:RuntimeError")

    def test_operation_names_map_to_independent_features(self):
        self.assertEqual(
            operation_feature("appfolio_inbox_poll"),
            "communication_inbox",
        )
        self.assertEqual(
            operation_feature("work_order_reconciliation"),
            "work_order_reconciliation",
        )
        self.assertEqual(
            operation_feature("update_work_order_schedule"),
            "work_order_writes",
        )
        self.assertEqual(
            operation_feature("send_vendor_appfolio_message"),
            "work_order_writes",
        )
        self.assertEqual(
            operation_feature("manual_work_order_scan"),
            "work_order_list",
        )


class FeatureCircuitBreakerTests(unittest.TestCase):
    def make_health(self, path, **kwargs):
        return AppFolioFeatureHealth(
            path, failure_threshold=3, cooldown_seconds=60, **kwargs
        )

    def test_feature_opens_only_after_threshold(self):
        with tempfile.TemporaryDirectory() as root:
            health = self.make_health(os.path.join(root, "health.db"))
            health.record_failure("communication_inbox", RuntimeError("one"))
            health.record_failure("communication_inbox", RuntimeError("two"))
            self.assertEqual(
                health.get("communication_inbox")["status"], "degraded"
            )
            health.record_failure("communication_inbox", RuntimeError("three"))
            self.assertEqual(
                health.get("communication_inbox")["status"], "disabled"
            )
            with self.assertRaises(FeatureCircuitOpen):
                health.before("communication_inbox")

    def test_feature_failure_does_not_disable_unrelated_feature(self):
        with tempfile.TemporaryDirectory() as root:
            health = self.make_health(os.path.join(root, "health.db"))
            for _ in range(3):
                health.record_failure(
                    "communication_inbox", RuntimeError("contract")
                )
            health.before("work_order_reconciliation")
            self.assertEqual(
                health.get("work_order_reconciliation")["status"],
                "degraded",
            )

    def test_circuit_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "health.db")
            first = self.make_health(path)
            for _ in range(3):
                first.record_failure("report_export", RuntimeError("contract"))
            restarted = self.make_health(path)
            with self.assertRaises(FeatureCircuitOpen):
                restarted.before("report_export")

    def test_cooldown_allows_half_open_probe(self):
        with tempfile.TemporaryDirectory() as root:
            current = [datetime(2026, 8, 14, tzinfo=timezone.utc)]
            health = self.make_health(
                os.path.join(root, "health.db"), now=lambda: current[0]
            )
            for _ in range(3):
                health.record_failure("work_order_detail", RuntimeError("x"))
            current[0] += timedelta(seconds=61)
            health.before("work_order_detail")
            self.assertEqual(
                health.get("work_order_detail")["status"], "degraded"
            )

    def test_success_resets_failure_count(self):
        with tempfile.TemporaryDirectory() as root:
            health = self.make_health(os.path.join(root, "health.db"))
            health.record_failure("report_export", RuntimeError("x"))
            state = health.record_success("report_export")
            self.assertEqual(state["status"], "healthy")
            self.assertEqual(state["consecutive_failures"], 0)

    def test_health_report_surfaces_disabled_feature_and_reason(self):
        with tempfile.TemporaryDirectory() as root:
            health = self.make_health(os.path.join(root, "health.db"))
            error = AppFolioUIContractError(
                "communication_inbox", "row_contract", "ambiguous_rows"
            )
            for _ in range(3):
                health.record_failure("communication_inbox", error)
            report = build_foundation_health_report(feature_health=health)
            item = next(
                value for value in report["subsystems"]
                if value["subsystem"] == "communication_inbox_polling"
            )
            self.assertEqual(item["status"], "disabled")
            self.assertIn("ambiguous_rows", item["reason"])

    def test_browser_manager_records_only_failing_feature(self):
        with tempfile.TemporaryDirectory() as root:
            health = AppFolioFeatureHealth(
                os.path.join(root, "health.db"), failure_threshold=1
            )
            manager = AppFolioBrowserManager(
                profile_dir=os.path.join(root, "profile"),
                downloads_dir=os.path.join(root, "downloads"),
                headless=True,
                playwright_factory=FakePlaywright,
                login_callback=lambda _page: True,
                feature_health=health,
            )
            with self.assertRaisesRegex(RuntimeError, "inbox contract"):
                with manager.operation("appfolio_inbox_poll"):
                    raise RuntimeError("inbox contract")
            self.assertEqual(
                health.get("communication_inbox")["status"], "disabled"
            )
            with manager.operation("work_order_reconciliation"):
                pass
            self.assertEqual(
                health.get("work_order_reconciliation")["status"], "healthy"
            )
            manager.close()

    def test_shared_operation_feature_scopes_are_independent(self):
        with tempfile.TemporaryDirectory() as root:
            health = AppFolioFeatureHealth(
                os.path.join(root, "health.db"), failure_threshold=1
            )
            manager = AppFolioBrowserManager(
                profile_dir=os.path.join(root, "profile"),
                downloads_dir=os.path.join(root, "downloads"),
                headless=True,
                playwright_factory=FakePlaywright,
                login_callback=lambda _page: True,
                feature_health=health,
            )
            with self.assertRaisesRegex(RuntimeError, "semantic failure"):
                with manager.feature_scope("communication_inbox"):
                    raise RuntimeError("semantic failure")
            with manager.feature_scope("work_order_reconciliation"):
                pass
            self.assertEqual(
                health.get("communication_inbox")["status"], "disabled"
            )
            self.assertEqual(
                health.get("work_order_reconciliation")["status"],
                "healthy",
            )

    def test_scheduled_shared_browser_uses_feature_scopes(self):
        source = (ROOT / "rachel_inbox_monitor.py").read_text()
        body = source[source.index("if inbox_poll_due or ") :]
        self.assertIn("track_feature_health=False", body)
        self.assertIn(
            'manager.feature_scope("communication_inbox")', body
        )
        self.assertRegex(
            body,
            r'manager\.feature_scope\(\s*"work_order_reconciliation"',
        )


class FoundationSafetyTests(unittest.TestCase):
    def test_zero_write_metrics_accept_all_zero(self):
        assert_zero_write_metrics({
            "workflow_actions_attempted": 0,
            "appfolio_writes_attempted": 0,
            "outbound_messages_attempted": 0,
        }, context="unit_test")

    def test_zero_write_metrics_reject_any_nonzero(self):
        with self.assertRaisesRegex(RuntimeError, "zero_write_invariant_failed"):
            assert_zero_write_metrics({
                "workflow_actions_attempted": 1,
            }, context="reconciliation")

    def test_feature_health_schema_is_part_of_durable_migration(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "memory.db")
            initialize_event_memory(path)
            conn = sqlite3.connect(path)
            try:
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(appfolio_feature_health)"
                    )
                }
            finally:
                conn.close()
            self.assertTrue({
                "feature", "status", "consecutive_failures",
                "last_reason", "retry_after", "last_success_at",
            }.issubset(columns))

    def test_active_appfolio_paths_import_central_adapter(self):
        for name in (
            "rachel_appfolio_browser.py",
            "rachel_appfolio_communication_sender.py",
            "rachel_appfolio_inbox.py",
            "rachel_scraper_v2.py",
            "rachel_vendor_lifecycle.py",
            "rachel_wo_triage.py",
            "rachel_work_order_scanner.py",
        ):
            with self.subTest(module=name):
                self.assertIn(
                    "rachel_appfolio_ui",
                    (ROOT / name).read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
