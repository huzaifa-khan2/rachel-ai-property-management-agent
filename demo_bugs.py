"""
Live, read-only reproduction of the AppFolio-UI-drift bugs found during
Step 3 live QA (Aug 24-25 2026), for demoing to Owner on a call.

Every check below calls the SAME centralized lookup (accessible_name /
combined_selector / unique_visible from rachel_appfolio_ui.py) that
Rachel's real production functions use — this is not a reimplementation,
it is the actual contract the code depends on, run against the live page
so Owner can see the real DOM on screen.

Correction (Aug 28 2026, re-verified live against real credentials):
bug 6 (vendor-assignment select2 widget "missing") was a false positive
caused by this demo script's own simplified "Edit" lookup clicking the
wrong one of two same-labeled "Edit" elements (the service-request edit
page, not the work-order edit form) — production's real dispatch code
already disambiguated this correctly. Bugs 1 and 2's original root-cause
theory (stray whitespace in the accessible name) was also wrong; the
real causes (missing href on bug 1's control, and the same href-based
Edit disambiguation for bug 2) are now fixed in rachel_appfolio_ui.py /
rachel_wo_triage.py and reflected in the checks below. Bug 3 was tested
against a work order that had, in the interim, actually been scheduled
(see its own Actions Log) — that invalidates the test, not necessarily
the underlying claim; re-verify against a not-yet-scheduled WO before
concluding AppFolio removed the Schedule action. Bug 5 (composer reveal
trigger) remains unresolved.

Safety: no .fill(), no Save/Send click, no Schedule-modal submission.
The only interactive step is opening/closing the "Actions" dropdown
(bug 3) and opening/canceling the inline "Edit" form on the unassigned WO
(bug 6) — both reveal-only UI toggles, never a write.

Usage:
    cd ~/client-work/rachel-clientco
    PYTHONPATH=. .venv/bin/python demo_bugs.py [UNASSIGNED_WO_URL]

UNASSIGNED_WO_URL is optional — needed only for bug 6 (vendor-assignment
widget). Without it, bugs 1-5 run and bug 6 is skipped with instructions.
You can also set it via RACHEL_DEMO_UNASSIGNED_WO_URL.
"""

from __future__ import annotations

import os
import re
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "unused-placeholder-for-import-only")

from rachel_appfolio_browser import get_appfolio_browser_manager
from rachel_appfolio_ui import (
    AppFolioReadAdapter,
    WorkOrderDetailPage,
    accessible_name,
    combined_selector,
    unique_visible,
)

WO_NUM = "1638-1"
WO_URL = (
    "https://clientco.appfolio.com"
    "/maintenance/service_requests/1542/work_orders/1871"
)
ROUTE_PATTERN = "/maintenance/service_requests/:request/work_orders/:work_order"

RESULTS: list[tuple[int, str, bool]] = []


def banner(n: int, title: str) -> None:
    print()
    print("=" * 70)
    print(f" BUG {n}/6 — {title}")
    print("=" * 70)


AUTOPACE = os.getenv("RACHEL_DEMO_AUTOPACE", "").strip().lower() in ("1", "true", "yes")


def verdict(n: int, title: str, confirmed: bool) -> None:
    RESULTS.append((n, title, confirmed))
    label = "BUG CONFIRMED (still broken) ✗" if confirmed else "NOT REPRODUCED — looks fixed ✓"
    print(f"VERDICT: {label}")
    if not AUTOPACE and n < 6:
        input("\n[Press Enter when ready to show bug "
              f"{n + 1}/6 ...] ")


def dump_candidates(locator, limit: int = 3) -> None:
    count = locator.count()
    print(f"Non-exact candidates found on page: {count}")
    for i in range(min(count, limit)):
        item = locator.nth(i)
        try:
            html = item.evaluate("el => el.outerHTML")[:300]
        except Exception:
            html = "<could not read outerHTML>"
        print(f"  [{i}] {html}")


def check_email_link(page) -> None:
    banner(1, 'Vendor "Email" action — CSS class lookup (corrected Aug 28 2026)')
    print(
        "Root cause found live: this control is an <a class=\"js-work-order-email\">"
        " with NO href, so it is never exposed with accessibility role \"link\" -"
        " the original role/name lookup could never find it regardless of text."
    )
    print('Production lookup: page.locator(combined_selector('
          '"work_order.vendor_email_action")) -> "a.js-work-order-email"')
    exact = page.locator(combined_selector("work_order.vendor_email_action"))
    exact_count = exact.count()
    print(f"Matches: {exact_count} (production requires exactly 1)")
    confirmed = exact_count != 1
    if confirmed:
        dump_candidates(exact)
    verdict(1, "Vendor Email action", confirmed)


def check_edit_control(page) -> None:
    banner(2, '"Edit" control — disambiguated by href absence (corrected Aug 28 2026)')
    print(
        "Root cause found live: TWO elements have exactly the text \"Edit\" - a "
        "service-request-level edit link (has an href) and this work order's own "
        "edit control (no href, a JS toggle). Same disambiguation the vendor-dispatch "
        "flow already used correctly."
    )
    print('Production lookup: page.locator(combined_selector('
          '"work_order.dispatch.edit_candidates")).filter(has_text=r"^\\s*Edit\\s*$")')
    exact = page.locator(
        combined_selector("work_order.dispatch.edit_candidates")
    ).filter(has_text=re.compile(r"^\s*Edit\s*$"))
    exact_count = exact.count()
    print(f"Matches: {exact_count} (production requires exactly 1)")
    confirmed = exact_count != 1
    if confirmed:
        broad = page.get_by_text("Edit", exact=False)
        dump_candidates(broad)
    verdict(2, "Edit control", confirmed)


def check_schedule_action(page) -> None:
    banner(3, '"Schedule" action in the Actions menu')
    print('Production lookup: unique_visible(page, "work_order.schedule_action") '
          '— candidates are a/li/button with text "Schedule"')
    actions = unique_visible(page, "work_order.actions")
    if not actions:
        print("Could not even find the Actions button — skipping, page may have changed.")
        verdict(3, "Schedule action", True)
        return
    actions.click()
    page.wait_for_timeout(800)
    sched = unique_visible(page, "work_order.schedule_action")
    confirmed = sched is None
    print(f"Exact 'Schedule' menu item found: {'yes' if sched else 'no'}")
    if confirmed:
        broad = page.get_by_text("Sched", exact=False)
        dump_candidates(broad)
    # close the menu again — no item clicked, nothing submitted
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    verdict(3, "Schedule action label", confirmed)


def check_resident_availability(page) -> None:
    banner(4, '"Resident Availability" section marker — exact text lookup')
    print('Production lookup: get_by_text(accessible_name('
          '"work_order.resident_availability_marker"), exact=True)')
    exact = page.get_by_text(
        accessible_name("work_order.resident_availability_marker"), exact=True
    )
    exact_count = exact.count()
    print(f"Exact matches: {exact_count} (production requires exactly 1)")
    confirmed = exact_count != 1
    if confirmed:
        broad = page.get_by_text(
            accessible_name("work_order.resident_availability_marker"), exact=False
        )
        dump_candidates(broad)
    verdict(4, "Resident Availability marker", confirmed)


def check_text_composer(page) -> None:
    banner(5, 'Tenant text composer — placeholder + visibility')
    print('Production lookup: unique_visible(page, "work_order.text_composer") '
          '— expects a visible input/textarea with placeholder containing '
          '"Enter message"')
    composer = unique_visible(page, "work_order.text_composer")
    confirmed = composer is None
    print(f"Composer found visible with expected placeholder: {'yes' if composer else 'no'}")
    if confirmed:
        placeholders = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('textarea, input'))
                .map(el => ({
                    tag: el.tagName,
                    placeholder: el.placeholder || null,
                    visible: !!(el.offsetWidth || el.offsetHeight),
                }))
                .filter(x => x.placeholder)
            """
        )
        print("Actual placeholders found on the live page:")
        for p in placeholders[:10]:
            print(f"  {p}")
    verdict(5, "Tenant text composer placeholder", confirmed)


def check_vendor_container(page, unassigned_wo_url: str | None) -> None:
    banner(6, "Vendor-assignment widget (select2) on an unassigned WO")
    if not unassigned_wo_url:
        print("SKIPPED — no unassigned-WO URL given.")
        print("Re-run with: PYTHONPATH=. .venv/bin/python demo_bugs.py <unassigned_wo_url>")
        print('(pick any currently-unassigned WO in AppFolio and pass its URL, '
              "or navigate to it in the persistent browser profile first)")
        return
    AppFolioReadAdapter(page, "work_order_writes").navigate_read(
        unassigned_wo_url, route_pattern=ROUTE_PATTERN, timeout=30000, attempts=2
    )
    page.wait_for_timeout(1500)
    # Must use the same href-absent disambiguation as check_edit_control -
    # a plain text match for "Edit" was the actual bug here (it grabbed
    # the wrong one of two "Edit" elements, landing on the *service
    # request* edit page instead of the *work order* edit form, which is
    # the only one containing the vendor select2 widget below).
    edit = page.locator(
        combined_selector("work_order.dispatch.edit_candidates")
    ).filter(has_text=re.compile(r"^\s*Edit\s*$"))
    try:
        edit.click()
        page.wait_for_timeout(1500)
    except Exception as exc:
        print(f"Could not open the Edit form on this WO: {type(exc).__name__}")
        return
    print('Production lookup: page.locator(combined_selector('
          '"work_order.dispatch.vendor_container")) → "#s2id_maintenance_work_order_party"')
    vendor_container = page.locator(
        combined_selector("work_order.dispatch.vendor_container")
    )
    vendor_count = vendor_container.count()
    print(f"Matches: {vendor_count} (production requires exactly 1)")
    confirmed = vendor_count != 1
    if confirmed:
        select2_anywhere = page.locator(
            '[class*="select2" i], [id*="select2" i]'
        ).count()
        print(
            "Any select2-related element anywhere on this page: "
            f"{select2_anywhere} (0 means AppFolio removed the widget entirely, "
            "not just renamed its id)"
        )
    verdict(6, "Vendor-assignment select2 widget", confirmed)
    # leave the form without saving/canceling explicitly — nothing was
    # filled, so navigating away next discards it safely.


def main() -> int:
    unassigned_wo_url = (
        sys.argv[1] if len(sys.argv) > 1 else os.getenv("RACHEL_DEMO_UNASSIGNED_WO_URL")
    )

    print("Rachel / AppFolio live bug demo — READ-ONLY, no Save/Send will be clicked.")
    print(f"Target WO: #{WO_NUM} → {WO_URL}")
    if unassigned_wo_url:
        print(f"Unassigned WO for bug 6: {unassigned_wo_url}")

    manager = get_appfolio_browser_manager()
    try:
        with manager.operation("read_only_bug_demo") as page:
            AppFolioReadAdapter(page, "work_order_detail").navigate_read(
                WO_URL, route_pattern=ROUTE_PATTERN, timeout=60000, attempts=2
            )
            page.wait_for_timeout(1500)
            detail = WorkOrderDetailPage(page)
            body_text = detail.body_text()
            if WO_NUM not in body_text:
                print(
                    f"WARNING: loaded page does not visibly contain WO #{WO_NUM}. "
                    "The hardcoded WO_URL at the top of this script may be stale — "
                    "update it to the current disposable test WO before demoing."
                )

            check_email_link(page)
            check_edit_control(page)
            check_schedule_action(page)
            check_resident_availability(page)
            check_text_composer(page)
            check_vendor_container(page, unassigned_wo_url)
    finally:
        manager.close()

    print()
    print("=" * 70)
    print(" SUMMARY")
    print("=" * 70)
    for n, title, confirmed in RESULTS:
        mark = "✗ CONFIRMED" if confirmed else "✓ fixed"
        print(f"  Bug {n}: {title:<40} {mark}")
    confirmed_count = sum(1 for _, _, c in RESULTS if c)
    print(f"\n{confirmed_count}/{len(RESULTS)} bugs still reproduce live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
