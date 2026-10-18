"""Central AppFolio UI contracts, read recovery, and feature isolation.

AppFolio's React UI is an external dependency.  Production modules should
refer to the semantic contracts in this module instead of defining new CSS
selectors in business logic.  The adapter deliberately supports retries only
for reads; consequential writes must verify their target and claim a durable
intent immediately before their first mutation.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit


log = logging.getLogger("rachel.appfolio_ui")


APPFOLIO_FEATURES = (
    "authentication_session",
    "communication_inbox",
    "communication_thread_reader",
    "communication_thread_sender",
    "work_order_list",
    "work_order_detail",
    "work_order_reconciliation",
    "work_order_writes",
    "report_export",
    "portfolio_ingestion",
)


# Selector candidates are ordered from semantic/accessible to structural.
# Feature modules may use only names from this registry for active AppFolio
# browser paths.  Sanitized contract fixtures refer to the same names.
APPFOLIO_SELECTORS = {
    "auth.email": (
        'input[autocomplete="username"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[id="username"]',
        'input[autocomplete="email"]',
    ),
    "auth.password": (
        'input[autocomplete="current-password"]',
        'input[type="password"]',
        'input[name="password"]',
    ),
    "auth.submit": (
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("Sign in")',
    ),
    "auth.two_factor_method": (
        'input[type="radio"][name="twoFactorMethod"]',
    ),
    "auth.two_factor_sms": (
        'input[type="radio"][name="twoFactorMethod"][value="sms"]',
    ),
    "auth.two_factor_sms_label": (
        'label:has-text("Receive code via SMS")',
    ),
    "auth.send_code": (
        'input[type="submit"][value="Send Verification Code"]',
        'button:has-text("Send Verification Code")',
        'button:has-text("Send Code")',
        'button:has-text("Get Code")',
    ),
    "auth.code": (
        'input[name="otp"]',
        'input[name="code"]',
        'input[placeholder*="code" i]',
        'input[placeholder*="digit" i]',
        'input[placeholder*="enter" i]',
        'input[type="number"]',
        'input[type="text"]',
    ),
    "auth.verify": (
        'button:has-text("Verify")',
        'button:has-text("Continue")',
        'button:has-text("Log in")',
        'button[type="submit"]',
        'input[type="submit"]',
    ),
    "communication.list_button": ("ul.list-group > button",),
    "communication.list_item": ("li.list-group-item",),
    "communication.thread_root": (
        "section:has(> main[role='list']):has(textarea):has(button)",
    ),
    "communication.history": ("main[role='list']",),
    "communication.message": ("[role='listitem']",),
    "communication.composer": (
        "section > div:has(textarea):has(button)",
    ),
    "communication.composer_textarea": ("textarea:visible",),
    "communication.send": ("button:has-text(\"Send\")",),
    "communication.back": (
        'button:has-text("Back to Inbox")',
        'a:has-text("Back to Inbox")',
    ),
    "work_order.list_link": ("a.js-work-order-number",),
    "work_order.any_detail_link": ('a[href*="/work_orders/"]',),
    "work_order.list_row_ancestor": (
        "xpath=ancestor::div[contains(@class, "
        "'js-work-order-list-row-')][1]",
    ),
    "work_order.pagination_controls": ("a, button",),
    "work_order.body": ("body",),
    # The work-order "Actions" menu toggle is a plain <div> with
    # data-bs-toggle="dropdown" (not a <button>/<a>), so a tag-based
    # has-text selector never matches it (confirmed against the live DOM
    # Aug 28 2026). Its one stable identity is this class.
    "work_order.actions": ("div.js-options-dropdown-toggle",),
    "work_order.schedule_action": (
        'a:has-text("Schedule")',
        'li:has-text("Schedule")',
        'button:has-text("Schedule")',
    ),
    # Old placeholder/name-based candidates matched a native input that no
    # longer exists (see the react-gears note below); a single precise id
    # is used instead of appending it, since unique_visible()'s identity
    # dedup treats two candidates that both match the same live element as
    # two separate matches and fails closed (confirmed live Aug 28 2026).
    "work_order.schedule_date": ("#start-date-input",),
    # AppFolio replaced the native <select> for these two fields with a
    # react-select combobox (confirmed against the live DOM Aug 28 2026):
    # the input itself is a search box whose value is always cleared, the
    # chosen value only ever appears in a sibling ".Select-value-label"
    # span, and picking a value means opening the dropdown and clicking a
    # role="menuitem" button rather than filling the input or calling
    # select_option().
    "work_order.schedule_time": ("#start-time-input",),
    "work_order.schedule_duration": ("#duration-input",),
    # The "Job Description" text next to this field is a floating label
    # with no <label> element and no aria-label/aria-labelledby, so
    # get_by_label() can never find it (confirmed live Sep 2 2026,
    # 0 matches) - AppFolio's own internal field name is "remarks", not
    # "description", and this id is the only stable identity.
    "work_order.job_description_field": (
        "#maintenance_work_order_remarks",
    ),
    # The Schedule modal first shows one radio per tenant-submitted
    # availability block (each up to several hours wide) plus a "Choose
    # different time" option — the exact-2-hour-window fields below only
    # exist in the DOM after that radio is selected (confirmed live Sep 2
    # 2026: #start-date-input/#start-time-input/#duration-input all had
    # zero matches until this was clicked).
    "work_order.schedule_choose_different_time": (
        'label:has-text("Choose different time")',
    ),
    "work_order.schedule_selected_value": (
        "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), "
        "' '), ' Select-control ')][1]//span[contains(@class, "
        "'Select-value-label')]",
    ),
    # The input's own visible surface is only ~5px wide (the react-select
    # shows the value/placeholder in a sibling span drawn on top of it), so
    # clicking the input directly is intercepted by that sibling - open the
    # dropdown by clicking the containing control instead.
    "work_order.schedule_control_container": (
        "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), "
        "' '), ' Select-control ')][1]",
    ),
    "work_order.schedule_option": ('button.Select-option[role="menuitem"]',),
    # Single precise candidate for the same identity-dedup reason as
    # work_order.schedule_date above.
    "work_order.schedule_save": ("button.js-schedule-save",),
    "work_order.notify_tenant": (
        'a:has-text("Notify Tenant")',
        'button:has-text("Notify Tenant")',
        'li:has-text("Notify Tenant")',
    ),
    # The work-order "Email" quick-action is an <a> with no href (a
    # JS-driven button, not a navigational link), so it is never exposed
    # with accessibility role "link" - a role/name lookup can never find
    # it regardless of matched text. Its one stable identity is this
    # class (confirmed against the live DOM Aug 28 2026).
    "work_order.vendor_email_action": ("a.js-work-order-email",),
    "work_order.email_body": ("#email_options_body",),
    "work_order.email_subject": ("#email_options_subject",),
    "work_order.email_send": ("#send_button",),
    # The Notify Tenant "Send BCC to" field is a select2 multi-value tag
    # widget (confirmed against the live DOM Aug 29 2026): the real form
    # field that gets submitted is the hidden #email_options_bcc input, but
    # it is only kept in sync with the visible chip list by select2's own
    # JS in response to real UI interaction (type + comma) - directly
    # setting its value would not reliably persist through Send. Rachel's
    # own address (rachel@clientco.example) is present here by AppFolio
    # default; adding Manager must never remove that existing chip.
    "work_order.email_bcc_container": ("#s2id_email_options_bcc",),
    "work_order.email_bcc_search_input": (
        "#s2id_email_options_bcc input.select2-input",
    ),
    "work_order.email_bcc_hidden": ("#email_options_bcc",),
    "work_order.form_ancestor": ("xpath=ancestor::form[1]",),
    "work_order.text_composer": (
        # Superseded (Aug 28 2026): this composer is real but permanently
        # hidden - it belongs to the vendor "notify" quick action's fixed
        # message dialog, not tenant texting. Tenant texting moved to a
        # centralized inbox; see the texting.* entries below. Kept only so
        # demo_bugs.py can keep documenting this composer's true nature.
        'input[placeholder*="Enter message" i]',
        'textarea[placeholder*="Enter message" i]',
        'input[placeholder*="type your text message" i]',
        'textarea[placeholder*="type your text message" i]',
    ),
    # Tenant texting (confirmed live Aug 28 2026): not an inline
    # work-order-page composer. The robust, universal trigger is the
    # tenant's own contact/People record page - one "send text" action
    # tied to that specific party (data-party-type="Tenant"), which opens
    # the same messaging widget whether or not a prior conversation
    # exists. (A separate /texting/inbox lists existing conversations, but
    # has no equivalent for a tenant with no conversation yet.)
    # js-contact-card-name-link is shared by every contact card on the
    # page (property, tenant, vendor) - the href pattern is what actually
    # identifies the tenant's own card specifically.
    "work_order.tenant_contact_link": (
        'a.js-contact-card-name-link[href*="selected_tenant"]',
    ),
    "texting.send_message_trigger": ("a.js-send-text-message",),
    "texting.composer": ("#messaging-input",),
    "texting.composer_container": (
        "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), "
        "' '), ' mt-3 ')][1]",
    ),
    "work_order.form_recipient_select": ("select",),
    "work_order.description": (
        'textarea[name*="description" i]',
    ),
    "work_order.issue_search": ('input[placeholder="Search..."]',),
    "work_order.dispatch.vendor_container": (
        "#s2id_maintenance_work_order_party",
    ),
    "work_order.dispatch.vendor_hidden": (
        "#maintenance_work_order_party",
    ),
    "work_order.dispatch.vendor_chosen": (".select2-chosen",),
    "work_order.dispatch.trade": (
        "#maintenance_work_order_maintenance_work_order_category_"
        "work_order_category",
    ),
    "work_order.dispatch.trade_options": ("option",),
    "work_order.dispatch.save": ("#save_button",),
    "work_order.dispatch.vendor_choice": ("a.select2-choice",),
    "work_order.dispatch.vendor_search": ("#s2id_autogen7_search",),
    "work_order.dispatch.vendor_results": (
        "#select2-results-7 li.select2-result-selectable:visible",
    ),
    "work_order.dispatch.persisted_vendor": (
        "#s2id_maintenance_work_order_party .select2-chosen",
    ),
    "work_order.dispatch.edit_candidates": ("a:visible:not([href])",),
    "work_order.following_table": ("xpath=following::table[1]",),
    "work_order.table_rows": ("tr",),
    "work_order.table_cells": ("th, td",),
    "report.actions": (
        'button:has-text("Actions")',
        'a:has-text("Actions")',
    ),
    "report.export_excel": (
        'a:has-text("Export as Excel")',
        'li:has-text("Export as Excel")',
        'button:has-text("Export as Excel")',
        '[role="menuitem"]:has-text("Excel")',
        'a:has-text("Excel")',
        'li:has-text("Excel")',
    ),
    "report.menu_items": ("a, button, li",),
    "report.rent_roll_ledger_link": (
        'a[href*="tenant_ledger"]',
    ),
    "report.virtual_scroll_viewport": (
        ".ag-body-viewport",
        ".ag-center-cols-viewport",
    ),
}


# Accessible names for exact Playwright get_by_role/get_by_label/get_by_text
# locators. Kept separate from APPFOLIO_SELECTORS (CSS/XPath candidate
# tuples consumed by page.locator()) because these are matched by
# Playwright's accessibility-tree lookups instead - a different mechanism,
# not a rewrite of an existing one. Centralized for the same reason as
# APPFOLIO_SELECTORS: an AppFolio label/control rename should require one
# edit here, not several scattered literals across workflow code.
APPFOLIO_ACCESSIBLE_NAMES = {
    "work_order.add_control": "+ Add",
    "work_order.job_description_label": "Job Description",
    "work_order.save_button": "Save",
    "work_order.resident_availability_marker": "Resident Availability",
    "work_order.send_button": "Send",
}


# The set of accessible names production code is expected to depend on.
# validate_accessible_name_registry() fails closed if any is missing, so an
# accidental deletion is caught offline instead of surfacing live.
REQUIRED_ACCESSIBLE_NAMES = frozenset(APPFOLIO_ACCESSIBLE_NAMES.keys())

# Accessible-name VALUES that legitimately repeat across distinct contract
# IDs (documented, deliberate). Empty today - every centralized accessible
# name currently maps to a distinct AppFolio label/control. Add an entry
# here only with a comment explaining why the repeat is intentional.
ACCEPTED_DUPLICATE_ACCESSIBLE_NAME_VALUES: frozenset = frozenset()


def accessible_name(name: str) -> str:
    try:
        return APPFOLIO_ACCESSIBLE_NAMES[name]
    except KeyError as error:
        raise AppFolioUIContractError(
            "ui_contracts", "accessible_name_lookup", f"unknown_name_{name}"
        ) from error




def work_order_issue_option(page_or_locator, issue_label: str):
    """
    Locate the exact AppFolio issue-option control for one business issue
    label. The workflow supplies only the business value (issue_label);
    this centralizes the underlying accessibility-role matching mechanism
    so a change to how AppFolio renders issue options requires one edit
    here, not a change at every call site. Matching is exact, matching the
    behavior this replaces verbatim.
    """
    return page_or_locator.get_by_role(
        "option", name=str(issue_label or "").strip(), exact=True
    )


def _accessible_names_source_duplicate_keys() -> list[str]:
    """
    Detect duplicate dict-literal keys in APPFOLIO_ACCESSIBLE_NAMES's own
    source. Python silently keeps only the last of a duplicate key in a
    dict literal, so the loaded dict can never reveal this - it must be
    checked from source. Offline-only: reads this module's own source
    file; no network, no browser.
    """
    try:
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=Path(__file__).name)
    except Exception:
        return ["accessible_names_source_unreadable"]
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "APPFOLIO_ACCESSIBLE_NAMES"
            and isinstance(node.value, ast.Dict)
        ):
            continue
        seen = set()
        duplicates = []
        for key_node in node.value.keys:
            if (
                isinstance(key_node, ast.Constant)
                and isinstance(key_node.value, str)
            ):
                if key_node.value in seen:
                    duplicates.append(key_node.value)
                seen.add(key_node.value)
        return [f"accessible_name_duplicate_key:{name}" for name in duplicates]
    return ["accessible_names_dict_not_found_in_source"]


def validate_accessible_name_registry() -> list[str]:
    """
    Offline-only structural validation of APPFOLIO_ACCESSIBLE_NAMES.

    Operates purely on the in-memory registry and this module's own source
    text - never launches a browser, never makes a network call. Returns a
    sanitized list of errors; an empty list is healthy.
    """
    errors = list(_accessible_names_source_duplicate_keys())
    for required in sorted(REQUIRED_ACCESSIBLE_NAMES):
        if required not in APPFOLIO_ACCESSIBLE_NAMES:
            errors.append(f"accessible_name_missing:{required}")
    for name, value in APPFOLIO_ACCESSIBLE_NAMES.items():
        if not isinstance(value, str) or not value.strip():
            errors.append(f"accessible_name_value_empty:{name}")
    value_to_names: dict = {}
    for name, value in APPFOLIO_ACCESSIBLE_NAMES.items():
        value_to_names.setdefault(value, []).append(name)
    for value, names in value_to_names.items():
        if (
            len(names) > 1
            and value not in ACCEPTED_DUPLICATE_ACCESSIBLE_NAME_VALUES
        ):
            errors.append(
                "accessible_name_value_duplicated:"
                + value + ":" + ",".join(sorted(names))
            )
    try:
        accessible_name("__validation_probe_unknown_name__")
        errors.append("accessible_name_lookup_did_not_fail_closed")
    except AppFolioUIContractError:
        pass
    return errors


WORK_ORDER_DISPATCH_REQUIRED_CHECKBOXES = {
    "maintenance_work_order_send_vendor_wo_link":
        "Email vendor a secure link to view this work order",
    "maintenance_work_order_send_vendor_text":
        "Text vendor a secure link to view this work order",
    "maintenance_work_order_require_vendor_accept_wo":
        "Request vendor to confirm receipt of work order",
}

WORK_ORDER_DISPATCH_PROTECTED_CHECKBOXES = (
    "maintenance_work_order_approved_by_owner",
    "maintenance_work_order_mark_ready_to_bill",
)


def selector_for_id(element_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", str(element_id or "")):
        raise AppFolioUIContractError(
            "ui_contracts", "selector_lookup", "invalid_element_id"
        )
    return "#" + str(element_id)


@dataclass(frozen=True)
class AppFolioDOMContract:
    feature: str
    route_patterns: tuple[str, ...]
    selector_names: tuple[str, ...]
    identity_invariants: tuple[str, ...]
    writes_allowed: bool = False


APPFOLIO_DOM_CONTRACTS = {
    "authentication": AppFolioDOMContract(
        "authentication_session",
        ("/", "/login", "/authenticate"),
        ("auth.email", "auth.password", "auth.submit"),
        (
            "authenticated shell OR exactly one complete login form",
            "2FA state must be recognized before any verification action",
        ),
    ),
    "communication_inbox": AppFolioDOMContract(
        "communication_inbox",
        ("/texting/inbox",),
        ("communication.list_button", "communication.list_item"),
        (
            "full inbox filter scope is verified",
            "polling extracts rows without opening a conversation",
        ),
    ),
    "communication_thread": AppFolioDOMContract(
        "communication_thread_reader",
        ("/texting/inbox/:conversation",),
        (
            "communication.thread_root",
            "communication.history",
            "communication.message",
            "communication.composer_textarea",
            "communication.send",
            "communication.back",
        ),
        (
            "one semantic thread root",
            "contact, role, context match the authorized list row",
            "one history, composer, Send control, and Back control",
            "post-parse semantic identity survives React node replacement",
        ),
    ),
    "work_order_list": AppFolioDOMContract(
        "work_order_list",
        ("/maintenance/service_requests/work_orders",),
        (
            "work_order.list_link",
            "work_order.list_row_ancestor",
            "work_order.pagination_controls",
        ),
        (
            "route is the work-order list",
            "every accepted WO link has one containing list row",
        ),
    ),
    "work_order_detail": AppFolioDOMContract(
        "work_order_detail",
        ("/maintenance/service_requests/:request/work_orders/:work_order",),
        ("work_order.body", "work_order.actions"),
        (
            "trusted AppFolio host and exact work-order route",
            "visible Work Order number matches the requested WO",
        ),
    ),
    "work_order_schedule": AppFolioDOMContract(
        "work_order_writes",
        ("/maintenance/service_requests/:request/work_orders/:work_order",),
        (
            "work_order.schedule_action",
            "work_order.schedule_date",
            "work_order.schedule_time",
            "work_order.schedule_duration",
            "work_order.schedule_save",
        ),
        (
            "exact WO identity verified before control discovery",
            "one durable two-hour window is double-confirmed",
            "write intent is claimed before the first form mutation",
        ),
        writes_allowed=True,
    ),
    "report_export": AppFolioDOMContract(
        "report_export",
        ("/buffered_reports/:report",),
        ("report.actions", "report.export_excel"),
        (
            "trusted AppFolio report route",
            "download is new to this run and parses as the expected workbook",
        ),
    ),
    "rent_roll": AppFolioDOMContract(
        "portfolio_ingestion",
        ("/buffered_reports/:report",),
        (
            "report.rent_roll_ledger_link",
            "report.virtual_scroll_viewport",
        ),
        (
            "trusted AppFolio report route",
            "ledger links have display labels and occupancy keys",
            "duplicate display names remain ambiguous",
        ),
    ),
}


class AppFolioUIContractError(RuntimeError):
    """A sanitized, feature-scoped UI contract failure."""

    def __init__(self, feature: str, stage: str, reason: str):
        self.feature = _safe_token(feature)
        self.stage = _safe_token(stage)
        self.reason = _safe_token(reason)
        super().__init__(f"{self.feature}:{self.stage}:{self.reason}")


def _safe_token(value: object, limit: int = 160) -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return text.strip("_")[:limit] or "unspecified"


def sanitized_failure_reason(error: BaseException) -> str:
    """Return a diagnostic reason without propagating PII-bearing text."""
    if isinstance(error, AppFolioUIContractError):
        return f"{error.stage}:{error.reason}"
    stage = getattr(error, "stage", "")
    reason = getattr(error, "reason", "")
    if stage or reason:
        return f"{_safe_token(stage)}:{_safe_token(reason)}"
    return f"exception_type:{_safe_token(type(error).__name__)}"


def selectors(name: str) -> tuple[str, ...]:
    try:
        return APPFOLIO_SELECTORS[name]
    except KeyError as error:
        raise AppFolioUIContractError(
            "ui_contracts", "selector_lookup", f"unknown_selector_{name}"
        ) from error


def combined_selector(name: str) -> str:
    return ", ".join(selectors(name))


def first_visible(page_or_locator, selector_name: str):
    """Return the first visible semantic candidate without retaining it."""
    for selector_value in selectors(selector_name):
        try:
            candidate = page_or_locator.locator(selector_value)
            for index in range(candidate.count()):
                item = candidate.nth(index)
                if item.is_visible():
                    return item
        except Exception:
            try:
                candidate = page_or_locator.query_selector(selector_value)
                if candidate and candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


def unique_visible(page_or_locator, selector_name: str):
    """Return one visible match across candidates, else fail closed with None."""
    matches = []
    seen = set()
    for selector_value in selectors(selector_name):
        try:
            candidate = page_or_locator.locator(selector_value)
            for index in range(candidate.count()):
                item = candidate.nth(index)
                try:
                    if not item.is_visible():
                        continue
                except Exception:
                    continue
                # Playwright locators for overlapping candidate selectors may
                # refer to the same element.  Prefer an element handle identity
                # when available; otherwise selector/index is conservative.
                identity = None
                try:
                    handle = item.element_handle()
                    identity = id(handle) if handle is not None else None
                except Exception:
                    pass
                key = identity or (selector_value, index)
                if key not in seen:
                    seen.add(key)
                    matches.append(item)
        except Exception:
            continue
    return matches[0] if len(matches) == 1 else None


def route_matches(url: str, route_pattern: str) -> bool:
    """Match an AppFolio route contract without inspecting query values."""
    path = urlsplit(str(url or "")).path.rstrip("/") or "/"
    pattern = route_pattern.rstrip("/") or "/"
    escaped = re.escape(pattern)
    escaped = re.sub(r":[A-Za-z0-9_]+", r"[^/]+", escaped)
    return bool(re.fullmatch(escaped, path, re.IGNORECASE))


def operation_feature(operation_name: str) -> str:
    name = str(operation_name or "").lower()
    if any(token in name for token in (
        "dispatch_vendor", "send_tenant_appfolio",
        "send_vendor_appfolio", "create_additional_work_order",
    )):
        return "work_order_writes"
    if "inbox" in name and "thread" not in name:
        return "communication_inbox"
    if "communication" in name or "thread" in name:
        return (
            "communication_thread_sender"
            if any(token in name for token in ("send", "reply"))
            else "communication_thread_reader"
        )
    if "reconciliation" in name:
        return "work_order_reconciliation"
    if "work_order_scan" in name or "weekday_scanner" in name:
        return "work_order_list"
    if "work_order" in name and any(
        token in name for token in ("schedule", "dispatch", "send", "edit")
    ):
        return "work_order_writes"
    if "work_order" in name or "triage" in name:
        return "work_order_detail"
    if "scrape" in name or "report" in name:
        return "report_export"
    if "login" in name or "auth" in name:
        return "authentication_session"
    return _safe_token(name or "appfolio_unknown_feature")


class FeatureCircuitOpen(RuntimeError):
    def __init__(self, feature: str, reason: str):
        self.feature = _safe_token(feature)
        self.stage = "circuit_breaker"
        self.reason = _safe_token(reason)
        super().__init__(f"{self.feature}:disabled:{self.reason}")


class AppFolioFeatureHealth:
    """Durable per-feature circuit breakers and structured health metrics."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: int = 900,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.db_path = db_path
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _resolved_db_path(self) -> str:
        return self.db_path or os.getenv(
            "RACHEL_DB_PATH", "/tmp/rachel_data.db"
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._resolved_db_path())
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appfolio_feature_health (
                feature TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_reason TEXT NOT NULL DEFAULT '',
                last_failure_at TEXT NOT NULL DEFAULT '',
                retry_after TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def before(self, feature: str) -> None:
        feature = _safe_token(feature)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM appfolio_feature_health WHERE feature = ?",
                (feature,),
            ).fetchone()
            if not row or row["status"] != "disabled":
                return
            retry_after = _parse_utc(row["retry_after"])
            if retry_after and self._now() >= retry_after:
                now = self._now().isoformat()
                conn.execute(
                    "UPDATE appfolio_feature_health SET status = 'degraded', "
                    "updated_at = ? WHERE feature = ?",
                    (now, feature),
                )
                conn.commit()
                return
            raise FeatureCircuitOpen(
                feature, row["last_reason"] or "failure_threshold_reached"
            )
        finally:
            conn.close()

    def record_success(self, feature: str, reason: str = "contract_verified") -> dict:
        feature = _safe_token(feature)
        now = self._now().isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO appfolio_feature_health (
                    feature, status, consecutive_failures, last_reason,
                    last_failure_at, retry_after, last_success_at, updated_at
                ) VALUES (?, 'healthy', 0, ?, '', '', ?, ?)
                ON CONFLICT(feature) DO UPDATE SET
                    status = 'healthy', consecutive_failures = 0,
                    last_reason = excluded.last_reason, retry_after = '',
                    last_success_at = excluded.last_success_at,
                    updated_at = excluded.updated_at
                """,
                (feature, _safe_token(reason), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(feature)

    def record_failure(self, feature: str, error: BaseException) -> dict:
        feature = _safe_token(feature)
        reason = sanitized_failure_reason(error)
        now_value = self._now()
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT consecutive_failures FROM appfolio_feature_health "
                "WHERE feature = ?",
                (feature,),
            ).fetchone()
            failures = int(existing[0] if existing else 0) + 1
            status = (
                "disabled" if failures >= self.failure_threshold else "degraded"
            )
            retry_after = (
                now_value + timedelta(seconds=self.cooldown_seconds)
            ).isoformat() if status == "disabled" else ""
            conn.execute(
                """
                INSERT INTO appfolio_feature_health (
                    feature, status, consecutive_failures, last_reason,
                    last_failure_at, retry_after, last_success_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?)
                ON CONFLICT(feature) DO UPDATE SET
                    status = excluded.status,
                    consecutive_failures = excluded.consecutive_failures,
                    last_reason = excluded.last_reason,
                    last_failure_at = excluded.last_failure_at,
                    retry_after = excluded.retry_after,
                    updated_at = excluded.updated_at
                """,
                (
                    feature, status, failures, reason, now_value.isoformat(),
                    retry_after, now_value.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        result = self.get(feature)
        log.warning(
            "AppFolioFeatureHealth %s",
            json.dumps(result, sort_keys=True),
        )
        return result

    def get(self, feature: str) -> dict:
        feature = _safe_token(feature)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM appfolio_feature_health WHERE feature = ?",
                (feature,),
            ).fetchone()
            if not row:
                return {
                    "feature": feature,
                    "status": "degraded",
                    "consecutive_failures": 0,
                    "reason": "not_yet_observed",
                    "retry_after": "",
                }
            result = dict(row)
            result["reason"] = result.pop("last_reason")
            return result
        finally:
            conn.close()

    def snapshot(self, features: Iterable[str] = APPFOLIO_FEATURES) -> dict:
        return {
            feature: self.get(feature)
            for feature in features
        }


def _parse_utc(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AppFolioReadAdapter:
    """Bounded read retry wrapper with feature-scoped health reporting."""

    def __init__(
        self,
        page,
        feature: str,
        *,
        health: Optional[AppFolioFeatureHealth] = None,
    ) -> None:
        self.page = page
        self.feature = feature
        self.health = health

    def read(
        self,
        stage: str,
        reader: Callable[[], object],
        *,
        attempts: int = 3,
        delay_seconds: float = 0.25,
        recover: Optional[Callable[[int, BaseException], None]] = None,
    ):
        """Retry a side-effect-free read; never retry a consequential write."""
        if self.health:
            self.health.before(self.feature)
        last_error: Optional[BaseException] = None
        for attempt in range(max(1, int(attempts))):
            try:
                result = reader()
                if self.health:
                    self.health.record_success(
                        self.feature, f"{_safe_token(stage)}_verified"
                    )
                return result
            except Exception as error:
                last_error = error
                if attempt + 1 >= max(1, int(attempts)):
                    break
                if recover:
                    recover(attempt + 1, error)
                if delay_seconds > 0:
                    try:
                        self.page.wait_for_timeout(int(delay_seconds * 1000))
                    except Exception:
                        time.sleep(delay_seconds)
        if last_error is None:
            last_error = AppFolioUIContractError(
                self.feature, stage, "read_failed_without_exception"
            )
        if self.health:
            self.health.record_failure(self.feature, last_error)
        raise last_error

    def navigate_read(
        self,
        url: str,
        *,
        route_pattern,
        timeout: int = 30000,
        attempts: int = 2,
    ) -> str:
        """`route_pattern` may be one pattern or an iterable of acceptable
        patterns — AppFolio sometimes redirects a bare service-request URL
        into that request's own work-order sub-page, which is still the
        correct destination, just a deeper route."""
        patterns = (
            [route_pattern] if isinstance(route_pattern, str)
            else list(route_pattern)
        )

        def navigate() -> str:
            self.page.goto(
                url, wait_until="domcontentloaded", timeout=timeout
            )
            raw_current_url = getattr(self.page, "url", "")
            current_url = (
                raw_current_url
                if isinstance(raw_current_url, str) and raw_current_url
                else str(url or "")
            )
            if not any(
                route_matches(current_url, pattern) for pattern in patterns
            ):
                raise AppFolioUIContractError(
                    self.feature, "page_identity", "route_mismatch"
                )
            return current_url

        return self.read("navigate_read", navigate, attempts=attempts)


class WorkOrderListPage:
    def __init__(self, page) -> None:
        self.page = page

    def links(self):
        return self.page.locator(combined_selector("work_order.list_link"))

    def row_for_link(self, link):
        return link.locator(selectors("work_order.list_row_ancestor")[0])

    def pagination_texts(self) -> list[str]:
        return self.page.locator(
            combined_selector("work_order.pagination_controls")
        ).all_inner_texts()

    def page_button(self, number: int):
        return self.page.get_by_role(
            "button", name=str(number), exact=True
        )

    def wait_for_different_first_work_order(
        self, previous_work_order: str, *, timeout: int = 20000
    ) -> None:
        self.page.wait_for_function(
            """
            ({selector, previous}) => {
                const current = document.querySelector(selector);
                return current && current.textContent.trim() !== previous;
            }
            """,
            arg={
                "selector": selectors("work_order.list_link")[0],
                "previous": previous_work_order,
            },
            timeout=timeout,
        )


class WorkOrderDetailPage:
    def __init__(self, page) -> None:
        self.page = page

    def body_text(self) -> str:
        return self.page.locator(
            combined_selector("work_order.body")
        ).inner_text()

    def has_semantic_identity(
        self, work_order_number: str, *, body_text: Optional[str] = None
    ) -> bool:
        rendered = self.body_text() if body_text is None else body_text
        return bool(re.search(
            rf"\bWork\s+Order\s*#{re.escape(str(work_order_number))}\b",
            rendered, re.IGNORECASE,
        ))


class CommunicationThreadPage:
    """DOM access for a thread after semantic reader verification."""

    def __init__(self, page) -> None:
        self.page = page

    def exact_composer_controls(self):
        roots = self.page.locator("section")
        matches = []
        for index in range(roots.count()):
            root = roots.nth(index)
            histories = root.locator(":scope > main[role='list']")
            if histories.count() != 1:
                continue
            direct_divs = root.locator(":scope > div")
            for div_index in range(direct_divs.count()):
                composer = direct_divs.nth(div_index)
                textarea = composer.locator(
                    combined_selector("communication.composer_textarea")
                )
                send = composer.get_by_role(
                    "button", name="Send", exact=True
                )
                if textarea.count() == 1 and send.count() == 1:
                    matches.append((textarea, send, root))
        return matches[0] if len(matches) == 1 else None


class ReportExportPage:
    def __init__(self, page) -> None:
        self.page = page

    def actions(self):
        return first_visible(self.page, "report.actions")

    def export_excel(self):
        return first_visible(self.page, "report.export_excel")

    def visible_menu_texts(self, limit: int = 20) -> list[str]:
        result = []
        try:
            controls = self.page.locator(
                combined_selector("report.menu_items")
            )
            items = [controls.nth(index) for index in range(controls.count())]
        except Exception:
            items = []
            for selector_value in selectors("report.menu_items"):
                try:
                    items.extend(self.page.query_selector_all(selector_value))
                except Exception:
                    continue
        for item in items:
            try:
                text_value = item.inner_text().strip()
                if item.is_visible() and text_value and len(text_value) < 50:
                    result.append(text_value)
            except Exception:
                continue
        return result[:limit]


class RentRollPage:
    """Semantic access to the virtualized rent-roll ledger list."""

    def __init__(self, page) -> None:
        self.page = page

    def ledger_items(self) -> list[dict]:
        return self.page.evaluate(
            """
            (selector) => Array.from(document.querySelectorAll(selector))
                .map((anchor) => ({
                    href: anchor.href || '',
                    text: (anchor.innerText || '').trim(),
                }))
            """,
            selectors("report.rent_roll_ledger_link")[0],
        )

    def scroll_step(self, pixels: int = 300) -> None:
        amount = max(1, min(int(pixels), 2000))
        self.page.evaluate(
            """
            ({selectors, amount}) => {
                window.scrollBy(0, amount);
                selectors.forEach((selector) => {
                    document.querySelectorAll(selector).forEach(
                        (element) => element.scrollBy(0, amount)
                    );
                });
            }
            """,
            {
                "selectors": list(selectors("report.virtual_scroll_viewport")),
                "amount": amount,
            },
        )


def validate_contract_registry() -> list[str]:
    """Return sanitized registry errors; an empty list is healthy."""
    errors = []
    for name, contract in APPFOLIO_DOM_CONTRACTS.items():
        if contract.feature not in APPFOLIO_FEATURES:
            errors.append(f"{name}:unknown_feature")
        if not contract.route_patterns:
            errors.append(f"{name}:route_missing")
        if len(contract.identity_invariants) < 2:
            errors.append(f"{name}:identity_invariants_insufficient")
        for selector_name in contract.selector_names:
            if selector_name not in APPFOLIO_SELECTORS:
                errors.append(f"{name}:selector_missing:{selector_name}")
    return errors


def fixture_directory() -> Path:
    return Path(__file__).resolve().parent / "appfolio_contract_fixtures"
