import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
PRODUCTION_FILES = (
    "main.py",
    "rachel_appfolio_ui.py",
    "rachel_foundation_health.py",
    "rachel_appfolio_communication_sender.py",
    "rachel_appfolio_inbox.py",
    "rachel_appfolio_write_intents.py",
    "rachel_communication_workflow.py",
    "rachel_inbox_monitor.py",
    "rachel_lease_window_processor.py",
    "rachel_maintenance_scheduling.py",
    "rachel_scraper_v2.py",
    "rachel_vendor_lifecycle.py",
    "rachel_wo_triage.py",
    "rachel_work_order_scanner.py",
    "rachel_event_memory.py",
)


class AppFolioBrowserArchitectureTests(unittest.TestCase):
    def test_every_active_smtp_sender_has_durable_claim_and_completion(self):
        inbox = self.read("rachel_inbox_monitor.py")
        lease = self.read("rachel_lease_tracker_v2.1.py")
        triage = self.read("rachel_wo_triage.py")
        bodies = {
            "inbox_email": inbox[
                inbox.index("def send_email("):
                inbox.index("# ── SMS via Google Voice")
            ],
            "inbox_sms": inbox[
                inbox.index("def send_sms("):
                inbox.index("# ── Database")
            ],
            "lease_email": lease[
                lease.index("def send_email("):
                lease.index("def load_rent_control(")
            ],
            "owner_qa_email": triage[
                triage.index("def send_owner_qa_email("):
                triage.index("def send_sms_via_gmail(")
            ],
            "management_email": triage[
                triage.index("def send_sms_via_gmail("):
                triage.index("def get_2fa_code(")
            ],
        }
        for name, body in bodies.items():
            with self.subTest(sender=name):
                self.assertIn("prepare_outbound_intent(", body)
                self.assertIn("claim_outbound_intent(", body)
                self.assertIn("complete_outbound_intent(", body)
                send_marker = (
                    "send_message(" if "send_message(" in body
                    else "sendmail("
                )
                self.assertLess(
                    body.index("claim_outbound_intent("),
                    body.index(send_marker),
                )
                self.assertGreater(
                    body.index("complete_outbound_intent("),
                    body.index(send_marker),
                )

    def test_shared_property_sheet_mutators_are_independently_gated(self):
        lease = self.read("rachel_lease_tracker_v2.1.py")
        boundaries = (
            (
                "def ensure_property_fact_workflow_columns(",
                "def sync_missing_properties_to_sheet(",
            ),
            (
                "def sync_missing_properties_to_sheet(",
                "def research_and_update_pending_sheet_rows(",
            ),
            (
                "def research_and_update_pending_sheet_rows(",
                "def get_rc(",
            ),
        )
        for start, end in boundaries:
            body = lease[lease.index(start):lease.index(end)]
            self.assertIn("property_fact_sheet_write_allowed()", body)
            first_write = min(
                (
                    position for marker in (
                        "sheet.update(", "sheet.append_rows("
                    )
                    for position in (body.find(marker),)
                    if position >= 0
                ),
                default=-1,
            )
            self.assertGreaterEqual(first_write, 0)
            self.assertLess(
                body.index("property_fact_sheet_write_allowed()"),
                first_write,
            )

    def test_operational_enablement_defaults_fail_closed(self):
        self.assertIn(
            'RACHEL_ENABLED = env_enabled("RACHEL_ENABLED", default=False)',
            self.read("main.py"),
        )
        self.assertIn(
            'RACHEL_ENABLED = env_bool("RACHEL_ENABLED", default=False)',
            self.read("rachel_safety.py"),
        )

    def test_production_never_installs_dependencies_at_runtime(self):
        for name in PRODUCTION_FILES:
            source = self.read(name)
            self.assertNotIn("pip3 install", source, name)
            self.assertNotIn("python -m pip install", source, name)

    def test_render_release_candidate_pins_appfolio_writes_off(self):
        render_config = (ROOT / "render.yaml").read_text()

        self.assertIn(
            "key: RACHEL_ENABLED\n        value: \"false\"",
            render_config,
        )
        self.assertIn("key: RACHEL_TEST_MODE\n        value: \"true\"", render_config)
        self.assertIn(
            "key: RACHEL_APPFOLIO_WRITES_ENABLED\n        value: \"false\"",
            render_config,
        )
        self.assertIn(
            "key: RACHEL_APPFOLIO_MESSAGING_ENABLED\n        value: \"false\"",
            render_config,
        )
        self.assertIn(
            "key: RACHEL_LEASE_EMAIL_DRY_RUN\n        value: \"true\"",
            render_config,
        )
        self.assertIn(
            "key: RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED\n"
            "        value: \"false\"",
            render_config,
        )
        for disabled_gate in (
            "RACHEL_OWNER_QA_MODE",
            "RACHEL_APPFOLIO_QA_ENABLED",
            "RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED",
            "RACHEL_SMS_ENABLED",
        ):
            self.assertIn(
                f"key: {disabled_gate}\n        value: \"false\"",
                render_config,
            )
        for required_secret in (
            "ANTHROPIC_API_KEY",
            "APPFOLIO_EMAIL",
            "APPFOLIO_PASSWORD",
            "RACHEL_EMAIL_PASSWORD",
            "RACHEL_GMAIL_USER",
            "RACHEL_GMAIL_PASSWORD",
        ):
            self.assertIn(
                f"key: {required_secret}\n        sync: false",
                render_config,
            )

    def read(self, name):
        return (ROOT / name).read_text(encoding="utf-8")

    def test_manager_is_only_production_chromium_launcher(self):
        offenders = [
            name
            for name in PRODUCTION_FILES
            if "launch_persistent_context" in self.read(name)
        ]
        self.assertEqual(offenders, [])
        self.assertEqual(
            self.read("rachel_appfolio_browser.py").count(
                "launch_persistent_context"
            ),
            1,
        )

    def test_main_does_not_subprocess_the_inbox_or_vendor_lifecycle(self):
        source = self.read("main.py")
        self.assertNotIn('run_script("rachel_inbox_monitor.py")', source)
        self.assertNotIn('run_script("rachel_vendor_lifecycle.py")', source)
        self.assertIn("from rachel_inbox_monitor import run_once", source)

    def test_superseded_standalone_recurring_entrypoints_are_disabled(self):
        inbox = self.read("rachel_inbox_monitor.py")
        watch = inbox[inbox.index("def run():") :]
        self.assertIn("Standalone inbox watch mode is disabled", watch)
        self.assertNotIn("while True:", watch)

        lifecycle = self.read("rachel_vendor_lifecycle.py")
        lifecycle_main = lifecycle[lifecycle.rindex(
            'if __name__ == "__main__":'
        ) :]
        self.assertIn(
            "Standalone vendor follow-up execution is disabled",
            lifecycle_main,
        )
        self.assertNotIn("check_vendor_followups()", lifecycle_main)

        triage = self.read("rachel_wo_triage.py")
        triage_main = triage[triage.rindex(
            'if __name__ == "__main__":'
        ) :]
        self.assertIn("no standalone live entry point", triage_main)
        self.assertNotIn("send_sms_via_gmail(", triage_main)

    def test_scheduler_uses_pacific_timezone_and_current_python_runtime(self):
        source = self.read("main.py")
        self.assertIn("[sys.executable, script_name]", source)
        for local_time in ("07:00", "08:00", "09:00"):
            self.assertIn(
                f'.at("{local_time}", "America/Los_Angeles")', source
            )

    def test_lease_email_dry_run_never_claims_verified_delivery(self):
        source = self.read("rachel_lease_tracker_v2.1.py")
        start = source.index("if dry_run:")
        end = source.index("if not RACHEL_EMAIL_USER", start)
        body = source[start:end]
        self.assertIn("return False", body)
        self.assertNotIn("return True", body)
        run_start = source.index("class RachelLeaseTracker:")
        run_end = source.index("def generate_weekly_digest", run_start)
        run_body = source[run_start:run_end]
        self.assertNotIn("return delivered or dry_run", run_body)

        scheduler = self.read("main.py")
        monthly_start = scheduler.index("def job_monthly_lease_cycle(")
        monthly_end = scheduler.index("def job_vendor_followups(")
        monthly_body = scheduler[monthly_start:monthly_end]
        self.assertLess(
            monthly_body.index("RACHEL_LEASE_EMAIL_DRY_RUN"),
            monthly_body.index("run_appfolio_scraper"),
        )

    def test_gmail_cycle_uses_independent_appfolio_due_checks(self):
        source = self.read("rachel_inbox_monitor.py")
        self.assertIn("_appfolio_inbox_poll_due()", source)
        self.assertIn("_work_order_reconciliation_due()", source)
        self.assertIn("RACHEL_APPFOLIO_INBOX_POLL_MINUTES", source)
        self.assertIn("RACHEL_WO_RECONCILIATION_MINUTES", source)
        self.assertIn("RACHEL_WO_RECONCILIATION_BACKOFF_MINUTES", source)

    def test_only_main_owns_recurring_reconciliation(self):
        source = self.read("rachel_work_order_scanner.py")
        self.assertIn("standalone recurring scanner is disabled", source)
        scheduler = source[source.index("def run_weekday_scheduler()") :]
        self.assertLess(scheduler.index("raise RuntimeError("), scheduler.index("while True:"))

    def test_reconciliation_has_no_workflow_or_emergency_route(self):
        scanner = self.read("rachel_work_order_scanner.py")
        start = scanner.index("def reconcile_logged_in_page(")
        end = scanner.index("def scan_logged_in_page(", start)
        body = scanner[start:end]

        self.assertNotIn("triage_work_order_details", scanner)
        self.assertNotIn("emergency_auto_triage", scanner)
        self.assertNotIn("request_manager_review(", body)
        self.assertNotIn("send_sms_via_gmail(", body)
        self.assertIn('"actions_attempted": 0', body)

    def test_work_order_navigation_is_exact_url_only(self):
        source = self.read("rachel_wo_triage.py")
        start = source.index("def navigate_to_wo(")
        end = source.index(
            "def _legacy_send_tenant_email_via_appfolio(", start
        )
        body = source[start:end]

        self.assertNotIn("global-search-input", body)
        self.assertNotIn("query_selector", body)
        self.assertIn("_validate_current_work_order_page", body)
        self.assertIn("_is_trusted_work_order_url", body)

    def test_no_personal_profile_defaults_in_production_files(self):
        offenders = []
        for name in PRODUCTION_FILES:
            source = self.read(name)
            if "/Users/" in source:
                offenders.append(name)
        self.assertEqual(offenders, [])
        self.assertNotIn("/Users/", self.read("rachel_setup.sh"))

    def test_login_diagnostics_do_not_log_page_contents(self):
        source = self.read("rachel_wo_triage.py")
        self.assertNotIn("2FA page diagnostic:", source)
        self.assertNotIn("[Login] Final page:", source)
        self.assertIn("Final URL (query-free)", source)

    def test_live_write_safety_gates_remain_before_manager_acquisition(self):
        triage = self.read("rachel_wo_triage.py")
        vendor = self.read("rachel_vendor_lifecycle.py")

        send_start = triage.index("def _send_appfolio_message(")
        dispatch_start = triage.index("def dispatch_vendor_to_wo(")
        vendor_message_start = triage.index("def send_vendor_appfolio_message(")
        vendor_reply_start = triage.index("def handle_manager_vendor_reply(")
        schedule_start = vendor.index("def update_wo_schedule(")
        schedule_end = vendor.index("def handle_tenant_schedule_reply(")

        send_body = triage[send_start:dispatch_start]
        dispatch_body = triage[dispatch_start:vendor_message_start]
        vendor_message_body = triage[vendor_message_start:vendor_reply_start]
        schedule_body = vendor[schedule_start:schedule_end]

        self.assertLess(
            send_body.index("appfolio_write_allowed("),
            send_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            send_body.index("prepare_outbound_intent("),
            send_body.index("get_appfolio_browser_manager()"),
        )
        exact_text_start = triage.index("def send_text_via_appfolio(")
        exact_text_end = triage.index("# ── Triage Logic", exact_text_start)
        exact_text_body = triage[exact_text_start:exact_text_end]
        self.assertEqual(exact_text_body.count("send.click()"), 1)
        self.assertLess(
            exact_text_body.index("claim_outbound_intent("),
            exact_text_body.index("composer.fill("),
        )
        self.assertLess(
            exact_text_body.index("claim_outbound_intent("),
            exact_text_body.index("send.click()"),
        )
        self.assertGreater(
            exact_text_body.index("complete_outbound_intent("),
            exact_text_body.index("send.click()"),
        )
        self.assertLess(
            dispatch_body.index("vendor_contact_allowed("),
            dispatch_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            dispatch_body.index("appfolio_write_allowed("),
            dispatch_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            dispatch_body.index("prepare_outbound_intent("),
            dispatch_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            dispatch_body.index("claim_outbound_intent("),
            dispatch_body.index("trade_select.select_option("),
        )
        self.assertLess(
            dispatch_body.index("claim_outbound_intent("),
            dispatch_body.index("save_button.click()"),
        )
        self.assertGreater(
            dispatch_body.index("complete_outbound_intent("),
            dispatch_body.index("save_button.click()"),
        )
        self.assertLess(
            vendor_message_body.index("vendor_contact_allowed("),
            vendor_message_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            vendor_message_body.index("appfolio_write_allowed("),
            vendor_message_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            vendor_message_body.index("prepare_outbound_intent("),
            vendor_message_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            vendor_message_body.index("claim_outbound_intent("),
            vendor_message_body.index("body_field.fill("),
        )
        self.assertLess(
            vendor_message_body.index("claim_outbound_intent("),
            vendor_message_body.index("send_button.click()"),
        )
        self.assertGreater(
            vendor_message_body.index("complete_outbound_intent("),
            vendor_message_body.index("send_button.click()"),
        )
        self.assertLess(
            schedule_body.index("if TEST_MODE:"),
            schedule_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            schedule_body.index("appfolio_write_allowed("),
            schedule_body.index("get_appfolio_browser_manager()"),
        )
        self.assertLess(
            schedule_body.index("verify_appfolio_schedule_attempt_claim("),
            schedule_body.index("get_appfolio_browser_manager()"),
        )
        self.assertEqual(schedule_body.count("save.click()"), 1)
        self.assertGreater(
            schedule_body.index("_schedule_window_visible_in_text("),
            schedule_body.index("save.click()"),
        )

    def test_duplicate_vendor_assignment_returns_before_notifications_or_save(self):
        source = self.read("rachel_wo_triage.py")
        start = source.index("def dispatch_vendor_to_wo(")
        end = source.index("def send_vendor_appfolio_message(")
        body = source[start:end]

        duplicate_guard = body.index("if already_selected:")
        duplicate_return = body.index("return True", duplicate_guard)
        notification_controls = body.index("for checkbox_id, label in (")
        save_click = body.index("save_button.click()")

        self.assertLess(duplicate_guard, duplicate_return)
        self.assertLess(duplicate_return, notification_controls)
        self.assertLess(duplicate_return, save_click)

    def test_inbox_poll_never_opens_a_conversation_thread(self):
        source = self.read("rachel_appfolio_inbox.py")
        start = source.index("def check_appfolio_inbox(")
        end = source.index(
            "def process_rachel_owned_appfolio_threads(", start
        )
        body = source[start:end]

        self.assertEqual(body.count("navigate_read("), 1)
        self.assertNotIn("_read_thread_event(", body)
        self.assertNotIn("_route_linked_communication(", body)
        self.assertIn('"thread_opened": False', body)
        self.assertIn('"list_observed_unopened"', body)
        self.assertLess(
            body.index("ensure_appfolio_inbox_full_monitoring_scope(page)"),
            body.index("_read_lightweight_inbox_rows(page)"),
        )

    def test_single_thread_read_is_explicit_and_polling_stays_list_only(self):
        source = self.read("rachel_appfolio_inbox.py")
        poll_start = source.index("def check_appfolio_inbox(")
        poll_end = source.index(
            "def process_rachel_owned_appfolio_threads(", poll_start
        )
        poll_body = source[poll_start:poll_end]
        read_start = source.index(
            "def inspect_one_appfolio_inbox_thread_read_only("
        )
        read_end = source.index("def _read_thread_event(", read_start)
        read_body = source[read_start:read_end]

        self.assertNotIn(
            "inspect_one_appfolio_inbox_thread_read_only", poll_body
        )
        self.assertNotIn("_click_exact_inbox_row", poll_body)
        self.assertIn("_click_exact_inbox_row", read_body)
        self.assertIn("_authorize_inbox_thread_open", read_body)
        self.assertLess(
            read_body.index("_authorize_inbox_thread_open"),
            read_body.index("_click_exact_inbox_row"),
        )
        self.assertIn("_read_verified_thread", read_body)
        self.assertIn("_INBOX_THREAD_READY_SCRIPT", source)
        verify_start = source.index("def _read_verified_thread(")
        verify_end = source.index("def _normalize_thread_messages(", verify_start)
        verify_body = source[verify_start:verify_end]
        self.assertLess(
            verify_body.index("frame.evaluate(_INBOX_THREAD_READY_SCRIPT)"),
            verify_body.index(
                "frame.evaluate(_INBOX_THREAD_READ_SCRIPT)"
            ),
        )
        self.assertIn("_return_to_inbox", read_body)
        self.assertIn("threads_opened = 0", poll_body)
        self.assertIn('"conversation_opens": threads_opened', poll_body)

    def test_owned_thread_reader_is_incremental_and_ownership_gated(self):
        source = self.read("rachel_appfolio_inbox.py")
        start = source.index(
            "def process_rachel_owned_appfolio_threads("
        )
        end = source.index("def process_triage_reply(", start)
        body = source[start:end]
        self.assertIn("classify_communication_ownership", body)
        self.assertIn("automatic_open_allowed", body)
        self.assertIn("appfolio_thread_selector_state", body)
        self.assertIn("thread_read_verified", body)
        self.assertIn(
            "inspect_one_appfolio_inbox_thread_read_only", body
        )
        self.assertNotIn("[:", body)
        self.assertIn('"human_or_unknown_thread_opens": 0', body)
        self.assertIn('"appfolio_writes_attempted": 0', body)
        self.assertIn('"outbound_messages_attempted": 0', body)

        monitor = self.read("rachel_inbox_monitor.py")
        poll = monitor.index("check_appfolio_inbox(page)")
        completed = monitor.index(
            "_mark_appfolio_inbox_poll_completed()", poll
        )
        owned = monitor.index(
            "process_rachel_owned_appfolio_threads(page)", completed
        )
        drafts = monitor.index(
            "process_pending_rachel_owned_response_drafts()", owned
        )
        self.assertLess(poll, completed)
        self.assertLess(completed, owned)
        self.assertLess(owned, drafts)
        # The draft-only auto-reply pass must run under its own feature
        # scope so a failure there can never gate/throttle the real
        # tenant/vendor reply routing in check_appfolio_inbox above.
        self.assertIn('feature_scope("communication_inbox_drafts")', monitor)

    def test_readonly_thread_qa_has_no_send_or_composer_api(self):
        source = self.read(
            "rachel_appfolio_inbox_thread_readonly_qa.py"
        )
        self.assertEqual(source.count("manager.operation("), 1)
        self.assertNotIn("send_sms_via_gmail", source)
        self.assertNotIn("process_triage_reply", source)
        self.assertNotIn("_route_linked_communication", source)
        self.assertIn('"fill"', source)
        self.assertIn('"type"', source)
        self.assertIn("blocked page action", source)

    def test_opened_thread_dom_diagnostic_is_exactly_gated_and_read_only(self):
        source = self.read(
            "rachel_appfolio_inbox_thread_dom_diagnostic.py"
        )
        self.assertEqual(source.count("manager.operation("), 1)
        self.assertEqual(source.count("guarded_page.goto("), 1)
        self.assertIn("_target_row_for_selector_key", source)
        self.assertIn("_authorize_inbox_thread_open", source)
        self.assertIn("_click_exact_inbox_row", source)
        self.assertLess(
            source.index("_authorize_inbox_thread_open"),
            source.index("_click_exact_inbox_row"),
        )
        self.assertIn(
            "diagnose_opened_appfolio_inbox_thread_structure", source
        )
        self.assertIn("_return_to_inbox", source)
        self.assertNotIn("_store_verified_thread", source)
        self.assertNotIn("send_sms_via_gmail", source)
        self.assertNotIn("process_triage_reply", source)
        self.assertNotIn("_route_linked_communication", source)
        self.assertIn('"appfolio_writes_attempted": 0', source)

        inbox_source = self.read("rachel_appfolio_inbox.py")
        start = inbox_source.index(
            "_INBOX_OPENED_THREAD_DOM_DIAGNOSTIC_SCRIPT ="
        )
        end = inbox_source.index('"""', start + 60)
        script = inbox_source[start:end]
        self.assertNotIn(".click()", script)
        self.assertNotIn("scrollTop =", script)
        self.assertNotIn("dispatchEvent", script)
        self.assertIn("legacy_thread_predicate", script)
        self.assertIn("repeated_sibling_stacks", script)
        self.assertIn("scroll_containers", script)
        self.assertIn("visible_element_inventory", script)

    def test_inbox_filter_reset_cannot_click_conversation_rows(self):
        source = self.read("rachel_appfolio_inbox.py")
        start = source.index("_INBOX_FILTER_SCOPE_RESET_SCRIPT =")
        end = source.index('"""', start + 40)
        script = source[start:end]
        self.assertIn("safeFilterClick", script)
        self.assertIn("element.closest('ul.list-group')", script)
        self.assertIn("element.querySelector('li.list-group-item')", script)
        self.assertEqual(script.count("element.click()"), 1)
        self.assertNotIn("window.location", script)

    def test_dom_diagnostic_runner_is_structural_and_non_clicking(self):
        source = self.read("rachel_appfolio_inbox_dom_diagnostic.py")
        self.assertNotIn(".click(", source)
        self.assertNotIn("dispatch_event", source.lower())
        self.assertNotIn("_read_thread_event", source)
        self.assertNotIn("check_appfolio_inbox", source)
        self.assertEqual(source.count("page.goto("), 1)
        self.assertIn("diagnose_appfolio_inbox_row_structure", source)

        inbox_source = self.read("rachel_appfolio_inbox.py")
        self.assertIn("element_inventory", inbox_source)
        self.assertIn("repeated_signature_groups", inbox_source)
        self.assertIn("vertical_sibling_stacks", inbox_source)
        self.assertIn("visibleElements.map", inbox_source)
        self.assertNotIn("const candidateParents", inbox_source)
        self.assertIn("identity_candidates", inbox_source)
        self.assertIn("unread_dot_candidates", inbox_source)
        self.assertIn("date_candidates", inbox_source)
        self.assertIn("react_structure", inbox_source)
        self.assertIn("<TEXT_SLOT_", inbox_source)

    def test_list_inventory_diagnostic_is_read_only_and_restores_scroll(self):
        source = self.read(
            "rachel_appfolio_inbox_list_inventory_diagnostic.py"
        )
        self.assertNotIn(".click(", source)
        self.assertNotIn("dispatch_event", source.lower())
        self.assertNotIn("check_appfolio_inbox", source)
        self.assertNotIn("_read_lightweight_inbox_rows", source)
        self.assertEqual(source.count("page.goto("), 1)
        self.assertIn("ul.list-group", source)
        self.assertIn("li.list-group-item", source)
        self.assertIn("scrollHeight", source)
        self.assertIn("clientHeight", source)
        self.assertIn("overflowY", source)
        self.assertIn("pane.scrollTop = originalScrollTop", source)
        self.assertIn("original_scroll_position_restored", source)
        self.assertIn("unread_state_inventory", source)
        self.assertIn("<NO_RAW_DOM_TEXT_RECORDED>", source)

    def test_combined_inbox_qa_uses_two_real_polls_and_one_manager(self):
        source = self.read(
            "rachel_appfolio_communication_inbox_combined_qa.py"
        )
        self.assertEqual(
            source.count("get_appfolio_browser_manager()"), 1
        )
        self.assertEqual(source.count("check_appfolio_inbox("), 2)
        self.assertEqual(source.count("manager.operation("), 2)
        self.assertIn("TemporaryDirectory", source)
        self.assertIn("same_context_reused", source)
        self.assertIn("duplicate_events_poll_2", source)
        self.assertIn("writes_messages_attempted", source)
        self.assertIn("RACHEL_ENABLED must be false", source)
        self.assertIn("blocked navigation outside the inbox list", source)
        self.assertNotIn("_read_thread_event", source)
        self.assertNotIn("process_triage_reply", source)
        self.assertNotIn("send_sms", source)


if __name__ == "__main__":
    unittest.main()
