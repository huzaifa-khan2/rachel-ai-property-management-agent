"""
LIVE DEMO SCRIPT for the call with Owner — runs Rachel's REAL production
functions (not a mirror/simulation) against whatever the newest disposable
test work order actually is. Nothing about the target WO is hardcoded:
this script discovers the WO number, its internal AppFolio URL, the
tenant name, the property address, and the issue trade by reading the
live tenant portal and manager pages itself, every time it runs.

The one fixed value is the test vendor ("First City Construction") —
that's a deliberate test fixture, not per-WO data: it's the single
disposable vendor account Owner set up in this sandbox, so there is
nothing to "discover" there.

Run this WITH Owner watching your screen. Each step prints what Rachel
did and why. Ctrl+C between steps if you want to pause and explain.

USAGE (set the 4 credential values from wherever you keep them, not here):
    cd ~/client-work/rachel-clientco
    export APPFOLIO_EMAIL="..."
    export APPFOLIO_PASSWORD="..."
    export RACHEL_GMAIL_USER="..."
    export RACHEL_GMAIL_PASSWORD="..."
    export TENANT_PORTAL_EMAIL="..."
    export TENANT_PORTAL_PASSWORD="..."
    PYTHONPATH=. .venv/bin/python demo_for_owner.py
"""

from __future__ import annotations

import os
import re

os.environ.setdefault("ANTHROPIC_API_KEY", "unused-placeholder-for-import-only")
# These three flags don't depend on discovery, so they're safe to set before
# any import — rachel_appfolio_browser's login callback does an inline
# `from rachel_wo_triage import login_appfolio` the first time it logs in,
# which happens during WO discovery below, long before the WO-specific
# details (WO number/ID/tenant/vendor) are known. That import freezes
# rachel_safety's module-level constants for the rest of the process, so
# these three must already be correct by then. The WO-specific ones are
# patched directly onto the already-imported rachel_safety module later,
# once discovery has actually found them.
os.environ.setdefault("RACHEL_ENABLED", "true")
os.environ.setdefault("RACHEL_TEST_MODE", "true")
os.environ.setdefault("RACHEL_APPFOLIO_QA_ENABLED", "true")

from playwright.sync_api import sync_playwright

from rachel_db_init import initialize_database
from rachel_appfolio_browser import get_appfolio_browser_manager
from rachel_appfolio_ui import AppFolioReadAdapter, WorkOrderDetailPage

TENANT_PORTAL_URL = "https://clientco.appfolio.com/connect"
WO_LIST_URL = (
    "https://clientco.appfolio.com"
    "/maintenance/service_requests/work_orders"
)
ROUTE_PATTERN = "/maintenance/service_requests/:request/work_orders/:work_order"

VENDOR_NAME = "First City Construction"  # the one disposable test vendor
VENDOR_PHONE = "N/A"

TRADE_KEYWORDS = {
    "plumbing": ("faucet", "toilet", "leak", "drain", "clog", "pipe", "sink"),
    "hvac": ("hvac", "heating", "cooling", "furnace", "air condition"),
    "electrical": ("electrical", "outlet", "breaker", "wiring", "light switch"),
    "appliance": ("dishwasher", "refrigerator", "washer", "dryer", "oven", "stove"),
    "pest_control": ("pest", "bug", "rodent", "roach", "ant", "mice"),
}


def infer_trade(issue_text: str) -> str:
    lowered = issue_text.lower()
    for trade, keywords in TRADE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return trade
    return "general"


def discover_latest_tenant_wo() -> dict:
    """Log into the tenant portal and read off the newest open WO."""
    tenant_email = os.environ["TENANT_PORTAL_EMAIL"]
    tenant_password = os.environ["TENANT_PORTAL_PASSWORD"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(TENANT_PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        page.locator("#email").fill(tenant_email)
        page.locator("#password").fill(tenant_password)
        page.locator("#kc-login").click()
        page.wait_for_timeout(3000)

        greeting = page.evaluate("() => document.body.innerText")
        name_match = re.search(r"HELLO\s*\n+\s*([^\n]+)", greeting)
        tenant_name = name_match.group(1).strip() if name_match else ""

        page.goto(
            "https://clientco.appfolio.com/connect/online_work_orders",
            wait_until="domcontentloaded", timeout=30000,
        )
        page.wait_for_timeout(2000)
        body = page.evaluate("() => document.body.innerText")
        browser.close()

    open_section = body.split("Closed Maintenance Requests")[0]
    entries = re.split(r"\n(?=\t?\n?[A-Z][a-zA-Z/ ]+\n(?:NEW|RECEIVED|TECHNICIAN))",
                        open_section)
    match = re.search(
        r"([A-Za-z0-9/ \-]+?)\n(NEW|RECEIVED|TECHNICIAN[^\n]*)\n+(.+?)\n+"
        r"Requested by.*?\n+Maintenance Request #(\d+-\d+)",
        open_section,
    )
    if not match:
        raise RuntimeError("Could not find any open maintenance request on the "
                            "tenant portal — has Owner submitted one?")

    category, status, description, wo_num = match.groups()
    return {
        "wo_num": wo_num.strip(),
        "category": category.strip(),
        "status": status.strip(),
        "description": description.strip(),
        "tenant_name": tenant_name,
    }


def resolve_manager_details(wo_num: str) -> dict:
    """Find the internal AppFolio URL and property address for wo_num."""
    manager = get_appfolio_browser_manager()
    try:
        with manager.operation("resolve_wo_url") as page:
            AppFolioReadAdapter(page, "work_order_list").navigate_read(
                WO_LIST_URL,
                route_pattern="/maintenance/service_requests/work_orders",
                timeout=30000, attempts=2,
            )
            page.wait_for_timeout(2500)
            links = page.evaluate(
                """
                (woNum) => Array.from(
                    document.querySelectorAll('a[href*="/work_orders/"]')
                )
                    .filter(a => a.innerText.trim() === woNum)
                    .map(a => a.getAttribute('href'))
                """,
                wo_num,
            )
            if not links:
                raise RuntimeError(
                    f"Could not find WO #{wo_num} in the manager work order list."
                )
            href = links[0].split("?")[0]
            if href.startswith("http"):
                wo_url = href
            else:
                wo_url = "https://clientco.appfolio.com" + href
            id_match = re.search(r"/service_requests/\d+/work_orders/(\d+)", href)
            if not id_match:
                raise RuntimeError(f"Unexpected work order URL shape: {href}")
            wo_id = id_match.group(1)

            AppFolioReadAdapter(page, "work_order_detail").navigate_read(
                wo_url, route_pattern=ROUTE_PATTERN, timeout=30000, attempts=2,
            )
            page.wait_for_timeout(1000)
            body = WorkOrderDetailPage(page).body_text()
    finally:
        manager.close()

    address_match = re.search(r"\nPROPERTY\n([^\n]+)", body)
    if not address_match:
        raise RuntimeError("Could not find PROPERTY address on the WO page.")

    return {
        "wo_url": wo_url,
        "wo_id": wo_id,
        "address": address_match.group(1).strip(),
    }


def step(n: int, title: str) -> None:
    print()
    print("=" * 70)
    print(f" STEP {n}: {title}")
    print("=" * 70)
    input("Press Enter to run this step live...")


def main() -> int:
    print("Discovering the newest test work order from the tenant portal...")
    tenant_wo = discover_latest_tenant_wo()
    print(f"Found WO #{tenant_wo['wo_num']} ({tenant_wo['category']}, "
          f"{tenant_wo['status']}) for tenant {tenant_wo['tenant_name']!r}")

    print("Resolving its internal AppFolio URL and property address...")
    details = resolve_manager_details(tenant_wo["wo_num"])
    print(f"URL: {details['wo_url']}")
    print(f"Address: {details['address']}")

    trade = infer_trade(tenant_wo["category"] + " " + tenant_wo["description"])
    print(f"Inferred trade: {trade}")

    wo_num = tenant_wo["wo_num"]
    wo_url = details["wo_url"]
    tenant = tenant_wo["tenant_name"]
    address = details["address"]
    # Use the short category (e.g. "Mold/Mildew"), not the tenant's raw
    # sentence-case description — inserting a full sentence mid-sentence
    # produced broken grammar like "...for the Yellow marks in my
    # bathroom. and will confirm...".
    issue = f"{tenant_wo['category'].lower()} issue"

    # rachel_safety was already imported (transitively, during discovery's
    # first login) with these four left at their empty defaults, so patch
    # them directly onto the live module instead of relying on os.environ.
    import rachel_safety
    rachel_safety.RACHEL_APPFOLIO_QA_WO_NUM = wo_num
    rachel_safety.RACHEL_APPFOLIO_QA_WO_ID = details["wo_id"]
    rachel_safety.RACHEL_APPFOLIO_QA_TENANT = tenant.lower()
    rachel_safety.RACHEL_APPFOLIO_QA_VENDOR = VENDOR_NAME.lower()

    from rachel_wo_triage import (
        dispatch_vendor_to_wo,
        send_tenant_email_via_appfolio,
        send_text_via_appfolio,
        send_vendor_appfolio_message,
    )

    tenant_email_message = (
        f"Hi {tenant.split()[0]}, thanks for the details on the {issue}. "
        "We're arranging someone to take a look and will confirm a "
        "scheduled time shortly."
    )
    tenant_text_message = (
        f"Hi {tenant.split()[0]}, we're arranging a technician for the "
        f"{issue} and will confirm a time window shortly. Thanks for your "
        "patience."
    )
    vendor_message = (
        f"Hi {VENDOR_NAME}, new work order #{wo_num} at {address} — "
        f"{issue}, {trade}. Please check the work order for tenant "
        "availability and confirm a window."
    )

    initialize_database()
    print("Database initialized.")

    step(1, "Dispatch vendor to work order (real AppFolio write)")
    result = dispatch_vendor_to_wo(
        wo_url=wo_url, wo_num=wo_num, vendor_name=VENDOR_NAME,
        vendor_phone=VENDOR_PHONE, trade=trade, tenant=tenant,
        address=address,
    )
    print(f"\ndispatch_vendor_to_wo result: {result}")

    step(2, "Notify tenant by email, with Manager BCC'd (real AppFolio write)")
    manager = get_appfolio_browser_manager()
    try:
        with manager.operation("demo_tenant_email") as page:
            AppFolioReadAdapter(page, "work_order_writes").navigate_read(
                wo_url, route_pattern=ROUTE_PATTERN, timeout=30000, attempts=2,
            )
            result = send_tenant_email_via_appfolio(
                page, tenant_email_message, wo_url=wo_url, wo_num=wo_num,
                tenant=tenant, vendor_name=VENDOR_NAME,
            )
    finally:
        manager.close()
    print(f"\nsend_tenant_email_via_appfolio result: {result}")

    step(3, "Text the tenant directly (real AppFolio write)")
    manager = get_appfolio_browser_manager()
    try:
        with manager.operation("demo_tenant_text") as page:
            result = send_text_via_appfolio(
                page, wo_url, tenant_text_message, tenant_has_cell=True,
                tenant=tenant, wo_num=wo_num, vendor_name=VENDOR_NAME,
            )
    finally:
        manager.close()
    print(f"\nsend_text_via_appfolio result: {result}")

    step(4, "Email the assigned vendor (real AppFolio write)")
    result = send_vendor_appfolio_message(
        wo_url, vendor_message, wo_num=wo_num, tenant=tenant,
        vendor_name=VENDOR_NAME,
    )
    print(f"\nsend_vendor_appfolio_message result: {result}")

    print(f"\nAll 4 steps complete. Reload WO #{wo_num} in AppFolio to show "
          "Owner the vendor assigned, the tenant email/text sent, and the "
          "vendor email sent — all done by Rachel's real code, live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
